import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_team import Request
from test_workflow_services import booking_data, fixture

from app.miniapp_security import AccessError
from app.models import (
    Hotel,
    HotelAlbumTariff,
    HotelEmployee,
    Notification,
    Receipt,
    Sale,
    SaleDiscountApproval,
    SaleDraftPhoto,
    User,
    UserRole,
)
from app.services.bookings import create_booking_record
from app.services.pricing import sale_quote
from app.services.sale_discounts import decide_discount, request_discount
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)


def test_whole_shoot_discount_requires_scoped_approval_and_preserves_album_deposit():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data() | {'deposit': '1000'})
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                session.add_all([User(id=3, tg_id=3, name='Scoped admin'),
                    UserRole(user_id=3, role='ADMIN'), HotelEmployee(user_id=3, hotel_id=1),
                    User(id=4, tg_id=4, name='Other admin'), UserRole(user_id=4, role='ADMIN'),
                    Hotel(id=2, name='Other hotel'), HotelEmployee(user_id=4, hotel_id=2),
                    HotelAlbumTariff(id=1, hotel_id=1, name='Маленький', price=10000),
                    Receipt(booking_id=booking.id, uploaded_by_id=1, purpose='DEPOSIT',
                        file_id='deposit', file_unique_id='deposit', expected_amount=1000,
                        verified_amount=1000, status='APPROVED')])
                await session.flush()
                approval = await request_discount(session, actor, booking.id, 3)
                assert approval.status == 'PENDING'
                assert (await request_discount(session, actor, booking.id, 3)).id == approval.id
                assert await session.scalar(select(func.count(SaleDiscountApproval.id))) == 1
                assert await session.scalar(select(func.count(Notification.id)).where(Notification.kind == 'sale_discount')) == 1
                with pytest.raises(ValueError, match='подтверждённая'):
                    await sale_quote(session, booking, 3, 3, discount_id=approval.id)
                for uid in (1, 2, 4):
                    with pytest.raises(AccessError):
                        await decide_discount(session, await session.get(User, uid), approval.id, 'APPROVED')
                admin = await session.get(User, 3)
                await decide_discount(session, admin, approval.id, 'APPROVED')
                assert (await decide_discount(session, admin, approval.id, 'APPROVED')).status == 'APPROVED'
                assert await session.scalar(select(func.count(Notification.id)).where(Notification.kind == 'sale_discount')) == 2
                with pytest.raises(AccessError, match='другое решение'):
                    await decide_discount(session, admin, approval.id, 'REJECTED')
                for sold, total in ((2, 3), (4, 4)):
                    with pytest.raises(ValueError, match='подтверждённая'):
                        await sale_quote(session, booking, sold, total, discount_id=approval.id)
                quote = await sale_quote(session, booking, 3, 3, discount_id=approval.id,
                                         album_id=1, album_quantity=2)
                assert quote['amount'] == '20600.00'
                assert [line['amount'] for line in quote['lines']] == ['1200.00', '-600.00', '20000.00']
                draft = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, draft, 'receipt', 'receipt', 'receipt-hash', {})
                assert await set_sale_counts(session, actor, draft, 3, 3,
                    discount_id=approval.id, album_id=1, album_quantity=2) == 20600
                assert draft.expected_amount == 19600
                # A later album edit cannot change an already agreed sale.
                (await session.get(HotelAlbumTariff, 1)).price = 15000
                for n in range(3):
                    session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id=f'f{n}',
                        telegram_unique_id=f'f{n}', storage_path=f'app:/f{n}', sha256=f'h{n}', byte_size=100))
                await session.flush()
                result = await complete_sale(session, actor, draft.id)
                assert result.sale.amount == 20600
                assert result.receipt.expected_amount == 19600
                saved = json.loads(result.sale.pricing_snapshot)
                assert saved['discountId'] == approval.id and saved['lines'][-1]['amount'] == '20000.00'
                assert result.sale.sold_photos == 3
                with pytest.raises(ValueError, match='после частичной'):
                    await sale_quote(session, booking, 3, 3, discount_id=approval.id)
                assert await session.scalar(select(func.count(Sale.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_bank_quote_uses_approved_discount_and_frozen_album(monkeypatch):
    from app.bank_payments import BankPayments
    from app.services.sale_discounts import SaleDiscounts
    monkeypatch.setattr('app.bank_payments.configured', lambda: True)
    monkeypatch.setenv('SBER_RETURN_URL', 'https://photo-boss.example/return')
    call = AsyncMock(return_value={'orderId': 'test-order', 'formUrl': 'https://ecomtest.sberbank.ru/payment/test'})
    monkeypatch.setattr('app.bank_payments.bank_call', call)
    async def run():
        engine, factory = await fixture()
        api = SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda request: request.body))
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                session.add(UserRole(user_id=1, role='OWNER'))
                booking = await create_booking_record(session, actor, booking_data() | {'deposit': '1000'})
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                session.add_all([HotelAlbumTariff(id=1, hotel_id=1, name='Маленький', price=10000),
                    Receipt(booking_id=booking.id, uploaded_by_id=1, purpose='DEPOSIT',
                        file_id='deposit', file_unique_id='deposit', expected_amount=1000,
                        verified_amount=1000, status='APPROVED')])
                await session.flush()
                approval = await request_discount(session, actor, booking.id, 10)
                await decide_discount(session, actor, approval.id, 'APPROVED')
                draft = await start_sale_draft(session, actor, {'OWNER'}, booking.id)
                await capture_draft_receipt(session, actor, draft, 'receipt', 'receipt', 'receipt-hash', {})
                await set_sale_counts(session, actor, draft, 10, 10,
                                      discount_id=approval.id, album_id=1, album_quantity=1)
                album = await session.get(HotelAlbumTariff, 1)
                album.price, album.active = 15000, False
                await session.commit()
                bid, approval_id = booking.id, approval.id
            with pytest.raises(AccessError) as invalid:
                await SaleDiscounts(api).decide(Request({'id': 1}, {'decision': []}, approval_id))
            assert invalid.value.status == 400
            body = {'bookingId': bid, 'sold': 10, 'total': 10, 'tariffId': None,
                    'discountId': approval_id, 'albumId': 1, 'albumQuantity': 1,
                    'amount': None, 'purpose': 'PURCHASE', 'requestKey': 'approved-discount-bank-1'}
            response = json.loads((await BankPayments(api).create(Request({'id': 1}, body))).text)
            # 10×400×50% + original10k album - confirmed1k deposit =11k.
            assert response['amount'] == '11000.00'
            assert call.await_args.args[1]['amount'] == 1100000
            replay = json.loads((await BankPayments(api).create(Request({'id': 1}, body))).text)
            assert replay['id'] == response['id'] and call.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_discount_rejected_or_unscoped_quote_cannot_be_reused():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                session.add(UserRole(user_id=1, role='OWNER'))
                booking = await create_booking_record(session, actor, booking_data())
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                await session.flush()
                approval = await request_discount(session, actor, booking.id, 10)
                await decide_discount(session, actor, approval.id, 'REJECTED')
                with pytest.raises(ValueError, match='подтверждённая'):
                    await sale_quote(session, booking, 10, 10, discount_id=approval.id)
                with pytest.raises(AccessError):
                    await request_discount(session, actor, booking.id, True)
                assert (await request_discount(session, actor, booking.id, 10)).status == 'REJECTED'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_discount_overpaid_deposit_shows_credit_without_negative_payment_or_refund():
    from app.services.receipts import payment_totals
    from app.workflow import Workflow
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                session.add(UserRole(user_id=1, role='OWNER'))
                booking = await create_booking_record(session, actor, booking_data() | {'deposit': '1000'})
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                session.add(Receipt(booking_id=booking.id, uploaded_by_id=1, purpose='DEPOSIT',
                    file_id='deposit', file_unique_id='deposit', expected_amount=1000,
                    verified_amount=1000, status='APPROVED'))
                await session.flush()
                approval = await request_discount(session, actor, booking.id, 3)
                await decide_discount(session, actor, approval.id, 'APPROVED')
                draft = await start_sale_draft(session, actor, {'OWNER'}, booking.id)
                assert await set_sale_counts(session, actor, draft, 3, 3, discount_id=approval.id) == 600
                assert draft.expected_amount is None
                for n in range(3):
                    session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id=f'f{n}',
                        telegram_unique_id=f'f{n}', storage_path=f'app:/f{n}', sha256=f'h{n}', byte_size=100))
                await session.flush()
                result = await complete_sale(session, actor, draft.id)
                assert result.sale.amount == 600 and result.sale.payment_status == 'PAID'
                assert result.receipt is None  # No negative payment and no automatic refund.
                assert tuple(await payment_totals(session, booking.id)) == (600, 1000, 0)
                assert await session.scalar(select(func.count(Receipt.id))) == 1
                await session.commit()
                bid = booking.id
            api = SimpleNamespace(engine=engine, today=lambda: date(2026, 9, 22))
            rows = json.loads((await Workflow(api).listing(Request({'id': 1, 'roles': ['OWNER']}))).text)['bookings']
            row = next(row for row in rows if row['id'] == bid)
            assert float(row['dueAmount']) == 0 and row['creditAmount'] == '400.00'
        finally:
            await engine.dispose()
    asyncio.run(run())
