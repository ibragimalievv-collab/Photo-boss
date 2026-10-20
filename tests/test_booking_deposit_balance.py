"""A declared deposit is never revenue or a payment until it is approved."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_workflow_services import booking_data, fixture

from app.models import PayrollEntry, Receipt, Sale, SaleDraftPhoto, User
from app.services.bookings import create_booking_record
from app.services.receipts import (
    expected_amount,
    money,
    payment_totals,
    refresh_payment_statuses,
)
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)
from app.workflow import Workflow


async def ready_booking(session, declared=None):
    data = booking_data()
    if declared is None:
        data.pop('deposit')
    else:
        data['deposit'] = declared
    actor = await session.get(User, 1)
    booking = await create_booking_record(session, actor, data)
    booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
    await session.flush()
    return actor, booking


async def prepared_purchase(session, actor, booking, *, number=1, sold=10):
    draft = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
    await capture_draft_receipt(session, actor, draft,
        f'receipt-{number}', f'receipt-unique-{number}', f'receipt-hash-{number}',
        {'status': 'not_configured'})
    assert await set_sale_counts(session, actor, draft, 150, sold) == sold * 400
    for index in range(sold):
        session.add(SaleDraftPhoto(draft_id=draft.id,
            telegram_file_id=f'selected-{number}-{index}',
            telegram_unique_id=f'selected-unique-{number}-{index}',
            storage_path=f'app:/selected/{number}/{index}.jpg',
            sha256=f'selected-hash-{number}-{index}', byte_size=100))
    await session.flush()
    return draft


@pytest.mark.parametrize('declared,expected', [
    (None, '1000.00'), ('0', '0.00'), ('500', '500.00'), ('1500', '1500.00'),
])
def test_default_and_custom_declared_deposits_do_not_count_as_received_money(declared, expected):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor, booking = await ready_booking(session, declared)
                assert money(booking.deposit) == Decimal(expected)
                assert await payment_totals(session, booking.id) == (0, 0, 0)
                draft = await prepared_purchase(session, actor, booking)
                assert money(draft.expected_amount) == Decimal('4000.00')
                result = await complete_sale(session, actor, draft.id)
                assert result.sale.amount == 4000
                assert result.sale.payment_status == 'UNPAID'
                assert result.receipt.expected_amount == Decimal('4000.00')
                assert await payment_totals(session, booking.id) == (4000, 0, 4000)
                assert await session.scalar(select(func.count(Receipt.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('status', ['PENDING', 'REJECTED'])
def test_unapproved_deposit_receipt_cannot_reduce_the_sale_balance(status):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor, booking = await ready_booking(session, '1000')
                # Even a saved amount from receipt recognition is not proof of payment.
                session.add(Receipt(booking_id=booking.id, uploaded_by_id=actor.id,
                    purpose='DEPOSIT', file_id='deposit', file_unique_id='deposit-unique',
                    expected_amount=1000, verified_amount=1000, status=status))
                await session.flush()
                draft = await prepared_purchase(session, actor, booking)
                assert money(draft.expected_amount) == Decimal('4000.00')
                result = await complete_sale(session, actor, draft.id)
                assert result.receipt.expected_amount == Decimal('4000.00')
                assert result.sale.payment_status == 'UNPAID'
                assert await payment_totals(session, booking.id) == (4000, 0, 4000)
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('received,due', [('500', '500'), ('1000', '0'), ('1500', '0')])
def test_partially_received_deposit_still_allows_the_remaining_deposit_to_be_paid(received, due):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor, booking = await ready_booking(session, '1000')
                session.add(Receipt(booking_id=booking.id, uploaded_by_id=actor.id,
                    purpose='DEPOSIT', file_id='deposit', file_unique_id='deposit-unique',
                    expected_amount=1000, verified_amount=Decimal(received), status='APPROVED'))
                # Pending/rejected photos cannot reduce the unreceived part of the deposit.
                for status in ('PENDING', 'REJECTED'):
                    session.add(Receipt(booking_id=booking.id, uploaded_by_id=actor.id,
                        purpose='DEPOSIT', file_id=status, file_unique_id=status,
                        expected_amount=500, verified_amount=500, status=status))
                await session.flush()
                assert await expected_amount(session, booking, 'DEPOSIT') == Decimal(due)
                assert await payment_totals(session, booking.id) == (0, Decimal(received), 0)
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('received,due,status', [
    ('500', '3500', 'PARTIAL'),
    ('1000', '3000', 'PARTIAL'),
    ('1500', '2500', 'PARTIAL'),
    ('4000', '0', 'PAID'),
    ('5000', '0', 'PAID'),
])
def test_actual_approved_deposit_controls_balance_without_changing_gross_sale(received, due, status):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor, booking = await ready_booking(session, '1000')
                draft = await prepared_purchase(session, actor, booking)
                assert money(draft.expected_amount) == Decimal('4000.00')
                # The owner confirms the actual bank amount after the draft was priced.
                deposit = Receipt(booking_id=booking.id, uploaded_by_id=actor.id,
                    purpose='DEPOSIT', file_id='deposit', file_unique_id='deposit-unique',
                    expected_amount=1000, verified_amount=Decimal(received), status='APPROVED')
                session.add(deposit)
                await session.flush()
                result = await complete_sale(session, actor, draft.id)
                assert result.sale.amount == 10 * 400
                assert result.sale.payment_status == status
                assert money(result.draft.expected_amount or 0) == Decimal(due)
                if Decimal(due):
                    assert result.receipt.expected_amount == Decimal(due)
                else:
                    assert result.receipt is None
                expected_totals = (Decimal(4000), Decimal(received), Decimal(due))
                # Repeated balance refreshes must not consume or duplicate the deposit.
                for _ in range(3):
                    await refresh_payment_statuses(session, booking.id)
                    assert await payment_totals(session, booking.id) == expected_totals
                    assert result.sale.payment_status == status
                assert money(booking.deposit) == Decimal('1000.00')
                assert money(deposit.verified_amount) == Decimal(received)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_deposit_and_final_payment_are_counted_once_across_completion_retries_and_followup():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor, booking = await ready_booking(session, '1000')
                session.add(Receipt(booking_id=booking.id, uploaded_by_id=actor.id,
                    purpose='DEPOSIT', file_id='deposit', file_unique_id='deposit-unique',
                    expected_amount=1000, verified_amount=1000, status='APPROVED'))
                await session.flush()
                first_draft = await prepared_purchase(session, actor, booking)
                first = await complete_sale(session, actor, first_draft.id)
                assert first.receipt.expected_amount == Decimal('3000.00')
                assert await payment_totals(session, booking.id) == (4000, 1000, 3000)
                first.receipt.status, first.receipt.verified_amount = 'APPROVED', Decimal(3000)
                await refresh_payment_statuses(session, booking.id)
                await session.commit()
                assert first.sale.payment_status == 'PAID'

                # A retried client completion returns the same sale, without new ledger rows.
                workflow = Workflow(SimpleNamespace(engine=engine))
                for _ in range(2):
                    repeated = await workflow.sale(session, actor, {'MANAGER'}, 'sale_complete',
                        {'draft': {'id': first_draft.id}, 'price': '400.00'}, None)
                    assert repeated['duplicate'] and repeated['saleId'] == first.sale.id
                    assert repeated['amount'] == '4000.00'
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(Receipt.id))) == 2
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                assert await payment_totals(session, booking.id) == (4000, 4000, 0)

                second_draft = await prepared_purchase(session, actor, booking, number=2, sold=2)
                assert money(second_draft.expected_amount) == Decimal('800.00')
                second = await complete_sale(session, actor, second_draft.id)
                assert second.sale.amount == 800
                assert second.sale.payment_status == 'UNPAID'
                assert second.receipt.expected_amount == Decimal('800.00')
                assert first.sale.payment_status == 'PAID'
                assert await payment_totals(session, booking.id) == (4800, 4000, 800)
                assert await session.scalar(select(func.sum(Sale.amount))) == 4800
                assert await session.scalar(select(func.count(Sale.id))) == 2
        finally:
            await engine.dispose()
    asyncio.run(run())
