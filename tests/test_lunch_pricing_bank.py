import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_team import Request
from test_workflow_replay import ACTOR, packet, setup
from test_workflow_services import booking_data, fixture

from app.bank_payments import reconcile
from app.hotel_pricing import HotelPricing
from app.miniapp_security import AccessError
from app.models import (
    BankPayment,
    Hotel,
    HotelEmployee,
    HotelTariff,
    Package,
    Receipt,
    SaleDraftPhoto,
    ShiftCheckIn,
    User,
    UserRole,
    WorkBreak,
)
from app.services.bookings import create_booking_record
from app.services.pricing import sale_quote, unit_price
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)
from app.work_breaks import WorkBreaks


def test_hotel_prices_packages_and_deposit_keep_gross_revenue_and_snapshot():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as s:
                actor = await s.get(User, 1)
                b = await create_booking_record(s, actor, booking_data() | {'deposit': '1000'})
                b.photographer_id, b.status = 2, 'READY_FOR_SALE'
                s.add_all([Hotel(id=2, name='Other'), HotelTariff(id=1, hotel_id=1, name='Кадр', kind='UNIT', price=400),
                           HotelTariff(id=2, hotel_id=2, name='Чужой', kind='UNIT', price=900),
                           HotelTariff(id=3, hotel_id=1, name='Десять', kind='PACKAGE', price=3200, photo_count=10)])
                await s.flush()
                with pytest.raises(ValueError, match='этом отеле'):
                    await sale_quote(s, b, 10, 150, 2)
                package_quote = await sale_quote(s, b, 10, 150, 3)
                assert package_quote['amount'] == '3200.00'
                assert package_quote['lines'] == [{'type': 'PACKAGE', 'name': 'Десять', 'price': '3200.00',
                                                   'quantity': 10, 'amount': '3200.00'}]
                with pytest.raises(ValueError, match='10 кадров'):
                    await sale_quote(s, b, 9, 150, 3)
                for price in (300, 400, 500):
                    unit = HotelTariff(hotel_id=1, name=f'Кадр {price}', kind='UNIT', price=price)
                    s.add(unit)
                    await s.flush()
                    quote = await sale_quote(s, b, 17, 150, unit.id)
                    assert quote['amount'] == f'{17 * price}.00'
                    assert quote['lines'][0]['quantity'] == 17
                invalid_unit = HotelTariff(hotel_id=1, name='Старая цена', kind='UNIT', price=450)
                s.add(invalid_unit)
                await s.flush()
                with pytest.raises(ValueError, match='300, 400 или 500'):
                    await sale_quote(s, b, 2, 150, invalid_unit.id)
                deposit = Receipt(booking_id=b.id, uploaded_by_id=1, purpose='DEPOSIT', file_id='deposit',
                    file_unique_id='deposit', expected_amount=1000, verified_amount=1000, status='APPROVED')
                s.add(deposit)
                draft = await start_sale_draft(s, actor, {'MANAGER'}, b.id)
                await capture_draft_receipt(s, actor, draft, 'receipt', 'unique', 'hash', {'status':'unavailable'})
                assert await set_sale_counts(s, actor, draft, 150, 10, tariff_id=1) == 4000
                assert draft.expected_amount == 3000
                (await s.get(HotelTariff, 1)).price = 500
                deposit.verified_amount = 1500
                for i in range(10):
                    s.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id=f'f{i}', telegram_unique_id=f'u{i}',
                        storage_path=f'app:/p{i}', sha256=f'h{i}', byte_size=100))
                await s.flush()
                result = await complete_sale(s, actor, draft.id)
                assert result.sale.amount == 4000 and result.receipt.expected_amount == 2500
                assert json.loads(result.sale.pricing_snapshot)['price'] == '400.00'
                assert b.status == 'SOLD'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_break_marks_replay_once_and_edits_are_scoped_versioned_and_audited(monkeypatch):
    moment = datetime(2026, 9, 22, 10, tzinfo=UTC).replace(tzinfo=None)
    monkeypatch.setattr('app.work_breaks.utc_now', lambda: moment)
    async def run():
        engine, factory, work = await setup()
        work.api.body = AsyncMock(side_effect=lambda r:r.body)
        try:
            async with factory() as s:
                s.add_all([HotelEmployee(hotel_id=1, user_id=1),
                    ShiftCheckIn(user_id=1, shift_date=date(2026, 9, 22), status='STARTED', started_at=moment),
                    User(id=3, tg_id=3, name='Admin'), UserRole(user_id=3, role='ADMIN')])
                await s.commit()
            body = packet('lunch', 'break_start', {'hotelId':1})
            first = json.loads((await work.execute(ACTOR, body)).text)
            assert first == json.loads((await work.execute(ACTOR, body)).text)
            with pytest.raises(AccessError, match='уже на обеде'):
                await work.execute(ACTOR, packet('lunch-double', 'break_start', {'hotelId':1}))
            edit = {'startedAt':'2026-09-22T09:50:00Z', 'endedAt':None, 'reason':'Уточнили время', 'revision':'2026-09-22T10:00:00Z'}
            svc = WorkBreaks(work.api)
            with pytest.raises(AccessError) as err:
                await svc.save(Request({'id':3,'roles':['ADMIN']}, edit, first['breakId']))
            assert err.value.status == 403
            with pytest.raises(AccessError):
                await svc.save(Request(ACTOR, edit, first['breakId']))
            async with factory() as s:
                s.add(UserRole(user_id=1, role='OWNER'))
                await s.commit()
            await svc.save(Request({'id':1,'roles':['OWNER']}, edit, first['breakId']))
            await work.execute(ACTOR, packet('return', 'break_end', {'hotelId':1}))
            async with factory() as s:
                row = await s.get(WorkBreak, first['breakId'])
                assert row.started_at.minute == 50 and row.ended_at == moment
                assert await s.scalar(select(func.count(WorkBreak.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_tariff_editor_checks_current_role_and_hotel_not_request_claim():
    async def run():
        engine, factory, work = await setup()
        work.api.body = AsyncMock(side_effect=lambda r:r.body)
        body = {'hotelId':1,'name':'Кадр','kind':'UNIT','price':'400','photoCount':None,'replaces':None}
        try:
            with pytest.raises(AccessError):
                await HotelPricing(work.api).save(Request({'id':1,'roles':['OWNER']}, body))
            async with factory() as s:
                s.add_all([UserRole(user_id=1, role='ADMIN'), HotelEmployee(user_id=1, hotel_id=1), Hotel(id=2,name='Other')])
                await s.commit()
            with pytest.raises(AccessError):
                await HotelPricing(work.api).save(Request({'id':1,'roles':['ADMIN']},body|{'hotelId':2}))
            await HotelPricing(work.api).save(Request({'id':1,'roles':['ADMIN']},body))
            with pytest.raises(AccessError) as err:
                await HotelPricing(work.api).save(Request({'id':1,'roles':['ADMIN']},body|{'name':'Недопустимая цена','price':'450'}))
            assert err.value.status == 400
            for invalid in ('NaN', 'Infinity', '-Infinity'):
                with pytest.raises(AccessError) as err:
                    await HotelPricing(work.api).save(Request({'id':1}, body|{'price':invalid}))
                assert err.value.status == 400
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_unit_price_seed_is_per_hotel_and_idempotent():
    async def run():
        from app.schema_updates import seed_default_unit_prices

        engine, factory = await fixture()
        try:
            async with engine.begin() as conn:
                first = await seed_default_unit_prices(conn)
                second = await seed_default_unit_prices(conn)
            async with factory() as session:
                rows = list(await session.scalars(select(HotelTariff).where(
                    HotelTariff.hotel_id == 1, HotelTariff.kind == 'UNIT', HotelTariff.active.is_(True))))
                assert {float(row.price) for row in rows} >= {300.0, 400.0, 500.0}
                assert second == 0
                assert first == 3
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_legacy_booking_price_survives_seeded_hotel_choices_and_explicit_tariffs():
    async def run():
        from app.schema_updates import seed_default_unit_prices

        engine, factory = await fixture()
        try:
            async with factory() as session:
                session.add(Hotel(id=2, name='Other'))
                await session.commit()
            async with engine.begin() as conn:
                await seed_default_unit_prices(conn)
            async with factory() as session:
                booking = await create_booking_record(session, await session.get(User, 1), booking_data())
                # The seed inserts 500 last; it is a choice, not a new default.
                assert await unit_price(session, booking) == '400.00'
                legacy = await sale_quote(session, booking, 10, 150)
                assert legacy['amount'] == '4000.00' and legacy['tariffId'] is None
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                actor = await session.get(User, 1)
                draft = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, draft, 'receipt', 'unique', 'hash', {'status': 'unavailable'})
                assert await set_sale_counts(session, actor, draft, 150, 10) == 4000
                chosen = await session.scalar(select(HotelTariff).where(
                    HotelTariff.hotel_id == 1, HotelTariff.price == 500))
                explicit = await sale_quote(session, booking, 10, 150, chosen.id)
                assert explicit['amount'] == '5000.00' and explicit['tariffId'] == chosen.id
                foreign = await session.scalar(select(HotelTariff).where(HotelTariff.hotel_id == 2))
                with pytest.raises(ValueError, match='этом отеле'):
                    await sale_quote(session, booking, 10, 150, foreign.id)
                chosen.price, chosen.active = 300, False
                historical = await sale_quote(session, booking, 10, 150, saved=json.dumps(explicit))
                assert historical['amount'] == '5000.00' and historical['price'] == '500.00'
                (await session.get(Package, booking.package_id)).price_per_photo = 450
                assert await set_sale_counts(session, actor, draft, 150, 10) == 4000
                with pytest.raises(ValueError, match='выберите тариф 300, 400 или 500'):
                    await sale_quote(session, booking, 10, 150)
                with pytest.raises(ValueError, match='Выберите действующий тариф'):
                    await sale_quote(session, SimpleNamespace(hotel_id=1, package_id=999), 10, 150)
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('test_mode', [False, True])
def test_bank_verification_is_exact_idempotent_and_test_money_never_enters_cash(monkeypatch,test_mode):
    monkeypatch.setenv('SBER_PAYMENTS_TEST','1' if test_mode else '0')
    response = {'errorCode':'0','orderNumber':'pb-test','orderStatus':2,'amount':300000,'currency':'643'}
    call = AsyncMock(return_value=response)
    monkeypatch.setattr('app.bank_payments.bank_call', call)
    async def run():
        engine,factory=await fixture()
        try:
            async with factory() as s:
                b=await create_booking_record(s,await s.get(User,1),booking_data())
                bill=BankPayment(booking_id=b.id,created_by_id=1,request_key='test-bank-request',payload_hash='hash',
                    order_number='pb-test',amount=3000,pricing_snapshot='{}',test_mode=test_mode)
                s.add(bill)
                await s.flush()
                call.return_value=response|{'amount':400000}
                with pytest.raises(AccessError,match='не совпал'):
                    await reconcile(s,bill)
                assert bill.receipt_id is None
                call.return_value=response
                await reconcile(s,bill)
                await reconcile(s,bill)
                assert await s.scalar(select(func.count(Receipt.id))) == (0 if test_mode else 1)
                assert bill.status == ('TEST_PAID' if test_mode else 'PAID')
                if not test_mode:
                    assert (await s.get(Receipt,bill.receipt_id)).verified_amount==3000
        finally:
            await engine.dispose()
    asyncio.run(run())
