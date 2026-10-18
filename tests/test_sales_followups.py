"""Repeat purchases append a transaction; settled money and old photos stay intact."""
import asyncio
import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_workflow_services import booking_data, fixture

from app.models import (
    Booking,
    Photo,
    Sale,
    SaleDraftPhoto,
    Shooting,
    User,
    UserRole,
    utc_now,
)
from app.services.bookings import create_booking_record
from app.services.receipts import payment_totals, refresh_payment_statuses
from app.services.sale_workflow import (
    already_purchased_photo,
    capture_draft_receipt,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)
from app.workflow import Workflow


async def prepared():
    engine, factory = await fixture()
    async with factory() as session:
        manager = await session.get(User, 1)
        booking = await create_booking_record(session, manager, booking_data())
        booking.photographer_id = 2
        booking.status = 'READY_FOR_SALE'
        shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == booking.id))
        shooting.status = 'READY_FOR_SALE'
        shooting.full_upload_completed_at = utc_now()
        session.add_all(Photo(shooting_id=shooting.id, file_id=f'original-{i}') for i in range(10))
        session.add_all([User(id=3, tg_id=3, name='Other manager'), UserRole(user_id=3, role='MANAGER')])
        await session.commit()
        return engine, factory, booking.id


async def purchase(session, booking_id, number, sold, *, repeated_hash=None):
    actor = await session.get(User, 1)
    draft = await start_sale_draft(session, actor, {'MANAGER'}, booking_id)
    await capture_draft_receipt(session, actor, draft, f'receipt{number}', f'unique{number}',
                                f'hash{number}', {'status': 'not_configured'})
    await set_sale_counts(session, actor, draft, 10, sold)
    for i in range(sold):
        digest = repeated_hash if i == 0 and repeated_hash else f'{number}-new-{i}'
        session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id=f'{number}:{i}',
            telegram_unique_id=f'{number}:{i}', storage_path=f'app:/sales/{draft.id}/{i}.jpg',
            sha256=digest, byte_size=100))
    await session.flush()
    return await complete_sale(session, actor, draft.id)


def test_followup_purchase_does_not_replace_first_sale_or_payment():
    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                first = await purchase(session, bid, 1, 2)
                first.receipt.status, first.receipt.verified_amount = 'APPROVED', Decimal(800)
                await refresh_payment_statuses(session, bid)
                await session.commit()
                original = (first.sale.id, first.sale.amount, first.sale.sold_photos, first.sale.commission)
                second = await purchase(session, bid, 2, 3)
                await session.commit()
                assert (first.sale.id, first.sale.amount, first.sale.sold_photos, first.sale.commission) == original
                assert first.sale.payment_status == 'PAID' and second.sale.payment_status == 'UNPAID'
                assert second.sale.amount == 1200 and second.receipt.expected_amount == 1200
                assert await payment_totals(session, bid) == (Decimal(2000), Decimal(800), Decimal(1200))
                second.receipt.status, second.receipt.verified_amount = 'APPROVED', Decimal(1200)
                await refresh_payment_statuses(session, bid)
                await session.commit()
                assert await payment_totals(session, bid) == (Decimal(2000), Decimal(2000), Decimal(0))
                assert await session.scalar(select(func.count(Sale.id))) == 2
                from app.models import PayrollEntry
                expected = sum(Decimal(str(sale.amount))*Decimal(sale.manager_percent_applied)/100
                               for sale in (first.sale, second.sale))
                assert Decimal(str(await session.scalar(select(func.sum(PayrollEntry.amount))))) == expected
                with pytest.raises(ValueError, match='завершена'):
                    await complete_sale(session, await session.get(User, 1), second.draft.id)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_new_pending_receipt_does_not_mark_old_unpaid_sale_as_paid():
    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                first = await purchase(session, bid, 1, 2)
                second = await purchase(session, bid, 2, 3)
                assert first.sale.payment_status == second.sale.payment_status == 'UNPAID'
                assert await payment_totals(session, bid) == (Decimal(2000), Decimal(0), Decimal(2000))
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_previously_purchased_original_cannot_be_sold_again():
    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                await purchase(session, bid, 1, 2)
                await session.commit()
                assert await already_purchased_photo(session, bid, '1-new-0')
                assert not await already_purchased_photo(session, bid, 'different-original')
                with pytest.raises(ValueError, match='уже купленный'):
                    await purchase(session, bid, 2, 1, repeated_hash='1-new-0')
                await session.rollback()
                assert await session.scalar(select(func.count(Sale.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('total,sold,message', [(11,1,'менять нельзя'), (10,9,'оставшееся')])
def test_finalized_count_and_remaining_stock_cannot_be_inflated(total, sold, message):
    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                await purchase(session, bid, 1, 2)
                actor = await session.get(User, 1)
                draft = await start_sale_draft(session, actor, {'MANAGER'}, bid)
                await capture_draft_receipt(session, actor, draft, 'new','new','new',{'status':'not_configured'})
                with pytest.raises(ValueError, match=message):
                    await set_sale_counts(session, actor, draft, total, sold)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_completed_order_allows_followup_but_cancelled_and_foreign_do_not():
    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                await purchase(session, bid, 1, 2)
                booking = await session.get(Booking, bid)
                booking.status = 'COMPLETED'
                await session.commit()
                with pytest.raises(ValueError, match='недоступна'):
                    await start_sale_draft(session, await session.get(User, 3), {'MANAGER'}, bid)
                await purchase(session, bid, 2, 1)
                booking.status = 'CANCELLED'
                with pytest.raises(ValueError, match='недоступна'):
                    await start_sale_draft(session, await session.get(User, 1), {'MANAGER'}, bid)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_history_endpoint_is_scoped_and_shows_additive_totals():
    class Request(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.query = {}
    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                await purchase(session, bid, 1, 2)
                await purchase(session, bid, 2, 3)
                await session.commit()
            api = Workflow(SimpleNamespace(engine=engine,today=lambda:date(2026,10,16)))
            request = Request(miniapp_actor={'id':1,'roles':['MANAGER']})
            request.query = {'booking':str(bid)}
            rows = json.loads((await api.listing(request)).text)['bookings']
            assert len(rows)==1 and rows[0]['soldTotal']==5 and rows[0]['salesAmount']=='2000.00'
            assert len(rows[0]['sales'])==2 and rows[0]['client']=='Guest'
            request['miniapp_actor']={'id':3,'roles':['MANAGER']}
            from app.miniapp_security import AccessError
            with pytest.raises(AccessError) as error:
                await api.listing(request)
            assert error.value.status==404
            request.query={}
            assert json.loads((await api.listing(request)).text)['bookings']==[]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_pending_sale_listing_keeps_counts_amount_and_responsible_employee():
    """An unfinished sale remains visible before any Sale row has been created."""
    class Request(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.query = {}

    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data())
                booking.photographer_id = 2
                booking.status = 'READY_FOR_SALE'
                draft = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, draft, 'receipt', 'unique', 'hash', {'status': 'unavailable'})
                from app.models import Receipt
                session.add(Receipt(booking_id=booking.id, uploaded_by_id=actor.id, purpose='DEPOSIT',
                    file_id='deposit', file_unique_id='deposit', expected_amount=Decimal(400),
                    verified_amount=Decimal(400), status='APPROVED'))
                await session.flush()
                await set_sale_counts(session, actor, draft, 150, 2)
                session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id='selected',
                    telegram_unique_id='selected', storage_path='app:/sales/one.jpg', sha256='one', byte_size=100))
                # Future bookings fill the recent-booking cap; the older open
                # draft must still be returned without an explicit lookup.
                session.add_all(Booking(hotel_id=booking.hotel_id, client_id=booking.client_id,
                    room=f'future-{n}', shoot_date=date(2030, 1, 1), shoot_time=booking.shoot_time,
                    package_id=booking.package_id, manager_id=actor.id) for n in range(201))
                await session.commit()
            api = Workflow(SimpleNamespace(engine=engine, today=lambda: date(2026, 10, 16)))
            response = json.loads((await api.listing(Request(miniapp_actor={'id': 1, 'roles': ['MANAGER']}))).text)
            assert len(response['bookings']) == 201
            row = next(row for row in response['bookings'] if row['id'] == booking.id)
            assert row['sales'] == [] and row['soldTotal'] == 0 and Decimal(row['salesAmount']) == 0
            assert row['totalFrames'] == 150
            assert row['draft']['status'] == 'AWAITING_SELECTED'
            assert row['draft']['selected'] == 1 and row['draft']['sold'] == 2
            assert row['draft']['total'] == 150 and row['draft']['amount'] == '800.00'
            assert row['draft']['expectedAmount'] == '400.00'
            assert row['draft']['createdByName'] == 'Manager'
            assert row['draft']['receiptStored'] is True
        finally:
            await engine.dispose()
    asyncio.run(run())
