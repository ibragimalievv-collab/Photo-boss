"""A queued walk-in sale creates one real booking without inventing a shoot."""
import asyncio
import json

import pytest
from sqlalchemy import func, select
from test_miniapp_release import signed
from test_sale_gallery_bridge import close, fixture

from app.models import (
    Booking,
    Client,
    Hotel,
    HotelEmployee,
    HotelTariff,
    OperationRequest,
    Package,
    PayrollEntry,
    Photo,
    Sale,
    SaleDraft,
    Shooting,
    User,
    UserRole,
)
from app.services.sale_workflow import capture_draft_receipt


async def members(f):
    async with f.factory() as session:
        session.add_all([HotelEmployee(hotel_id=1, user_id=i) for i in (1, 2)])
        await session.commit()


async def start(f, *, guest=None, price='400.00', actor=1, roles=None, key='inline-sale-start-0001'):
    body = {'actorId': actor, 'date': '2026-09-22', 'key': key, 'kind': 'sale_start_inline',
            'data': {'guest': guest or {'name': 'Гость без записи', 'hotelId': 1, 'photographerId': 2}, 'price': price}}
    response = await f.work.execute({'id': actor, 'roles': roles or ['MANAGER']}, body)
    return json.loads(response.text)


def test_inline_start_replay_is_atomic_and_completion_uses_real_photographer(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await members(f)
            async with f.factory() as session:
                original_bookings = await session.scalar(select(func.count(Booking.id)))
                original_clients = await session.scalar(select(func.count(Client.id)))
                original_photos = await session.scalar(select(func.count(Photo.id)))
            first, replay = await start(f), await start(f)
            assert first == replay
            f.did = first['draftId']
            async with f.factory() as session:
                booking = await session.get(Booking, first['bookingId'])
                shoot = await session.scalar(select(Shooting).where(Shooting.booking_id == booking.id))
                draft = await session.get(SaleDraft, f.did)
                assert booking.status == shoot.status == 'READY_FOR_SALE'
                assert (booking.manager_id, booking.photographer_id, booking.deposit, booking.room) == (1, 2, 0, '')
                assert str(booking.shoot_date) == '2026-09-22' and shoot.full_upload_completed_at is None
                assert not await session.scalar(select(func.count(Photo.id)).where(Photo.shooting_id == shoot.id))
                assert await session.scalar(select(func.count(Booking.id))) == original_bookings+1
                assert await session.scalar(select(func.count(Client.id))) == original_clients+1
                assert await session.scalar(select(func.count(Photo.id))) == original_photos
                await capture_draft_receipt(session, await session.get(User, 1), draft,
                    'inline-receipt', 'inline-receipt-unique', 'inline-receipt-hash', {'status': 'not_configured'})
                await session.commit()
            counts = {'actorId': 1, 'date': '2026-09-22', 'key': 'inline-counts-0000001',
                'kind': 'sale_counts', 'data': {'draft': {'key': 'inline-sale-start-0001'},
                    'price': '400.00', 'total': 1, 'sold': 1}}
            await f.work.execute({'id': 1, 'roles': ['MANAGER']}, counts)
            last = await f.upload('blue')
            assert last['gallery']['state'] == 'payment_pending'
            async with f.factory() as session:
                sale = await session.get(Sale, last['saleId'])
                assert sale.credited_user_id == 2 and sale.commission_finalized_at is None and sale.commission == 0
                assert sale.manager_payroll_entry_id is not None
                assert sale.source_draft_id == f.did and sale.payment_status != 'PAID'
            assert '/Photo#2/' in f.writes[-1][1] and '_Гость_без_записи_' in f.writes[-1][1]
            second = await start(f, key='inline-sale-independent-0002')
            assert second['bookingId'] != first['bookingId'] and second['draftId'] != first['draftId']
        finally:
            await close(f)
    asyncio.run(run())


@pytest.mark.parametrize('actor,roles', [(2, ['PHOTOGRAPHER']), (4, ['OWNER'])])
def test_inline_seller_without_manager_role_has_no_manager_commission(tmp_path, monkeypatch, actor, roles):
    async def run():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await members(f)
            result = await start(f, actor=actor, roles=roles,
                guest={'name': 'Фактический гость', 'hotelId': 1,
                       **({'photographerId': 2} if actor != 2 else {})})
            from app.models import SaleDraftPhoto
            from app.services.sale_workflow import complete_sale, set_sale_counts
            async with f.factory() as session:
                seller = await session.get(User, actor)
                draft = await session.get(SaleDraft, result['draftId'])
                await capture_draft_receipt(session, seller, draft, 'receipt-inline', 'unique-inline',
                                            'hash-inline', {'status': 'not_configured'})
                await set_sale_counts(session, seller, draft, 1, 1)
                session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id='app:/inline.jpg',
                    telegram_unique_id='inline', storage_path='app:/inline.jpg', sha256='a'*64, byte_size=123))
                await session.flush()
                sale = (await complete_sale(session, seller, draft.id)).sale
                assert sale.manager_payroll_entry_id is None and sale.manager_percent_applied == '0'
                assert not await session.scalar(select(func.count(PayrollEntry.id)))
                assert sale.credited_user_id == 2 and sale.commission_finalized_at is None
                assert json.loads(sale.pricing_snapshot)['managerCommissionEligible'] is False
                await session.commit()
        finally:
            await close(f)
    asyncio.run(run())


def test_inline_rejects_unscoped_hotel_wrong_photographer_and_rolls_price_failure_back(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await members(f)
            async with f.factory() as session:
                session.add(Hotel(id=2, name='Чужой отель'))
                await session.commit()
                before = {model: await session.scalar(select(func.count(model.id)))
                          for model in (Client, Booking, SaleDraft, OperationRequest)}
            for guest, price in [({'name': 'Нет доступа', 'hotelId': 2, 'photographerId': 2}, '400.00'),
                                 ({'name': 'Не фотограф', 'hotelId': 1, 'photographerId': 3}, '400.00'),
                                 ({'name': 'Нет выбора', 'hotelId': 1}, '400.00'),
                                 ({'name': 'Неверная цена', 'hotelId': 1, 'photographerId': 2}, '300.00')]:
                with pytest.raises(ValueError):
                    await start(f, guest=guest, price=price)
            async with f.factory() as session:
                assert {m: await session.scalar(select(func.count(m.id))) for m in before} == before
        finally:
            await close(f)
    asyncio.run(run())


def test_inline_uses_unambiguous_hotel_rate_or_explicit_legacy_package(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await members(f)
            async with f.factory() as session:
                session.add(Package(id=2, name='Другая цена', price_per_photo=500))
                await session.commit()
            with pytest.raises(ValueError, match='фиксированную цену'):
                await start(f)
            chosen = await start(f, price='500.00', guest={
                'name': 'Явный пакет', 'hotelId': 1, 'photographerId': 2, 'packageId': 2,
                'date': '2026-09-21', 'time': '18:45'})
            async with f.factory() as session:
                b = await session.get(Booking, chosen['bookingId'])
                assert b.package_id == 2 and json.loads(b.pricing_snapshot)['price'] == '500.00'
                assert str(b.shoot_date) == '2026-09-21' and str(b.shoot_time) == '18:45:00'
                tariff = HotelTariff(hotel_id=1, name='Фиксированная', kind='UNIT', price=300)
                session.add(tariff)
                await session.flush()
                (await session.get(Hotel, 1)).fixed_unit_tariff_id = tariff.id
                await session.commit()
            fixed = await start(f, price='300.00', key='inline-hotel-fixed-0002')
            async with f.factory() as session:
                b = await session.get(Booking, fixed['bookingId'])
                assert json.loads(b.pricing_snapshot)['source'] == 'hotel_fixed'
                assert json.loads(b.pricing_snapshot)['price'] == '300.00'
        finally:
            await close(f)
    asyncio.run(run())


@pytest.mark.parametrize('extra_role', ['OWNER', 'ADMIN', 'MANAGER'])
def test_combined_photographer_role_keeps_explicit_real_photographer_choice(tmp_path, monkeypatch, extra_role):
    async def run():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await members(f)
            from app.services.inline_sale import options
            async with f.factory() as session:
                session.add_all([User(id=5, tg_id=5, name='Other real photographer'),
                    UserRole(user_id=5, role='PHOTOGRAPHER'),
                    HotelEmployee(hotel_id=1, user_id=5), UserRole(user_id=2, role=extra_role)])
                await session.commit()
                policy = await options(session, 2, {'PHOTOGRAPHER', extra_role})
                assert policy['defaultPhotographerId'] is None and not policy['photographerLocked']
                assert {p['id'] for p in policy['photographers']} == {2, 5}
            with pytest.raises(ValueError, match='фотографа'):
                await start(f, actor=2, roles=['PHOTOGRAPHER', extra_role], guest={'name': 'Гость', 'hotelId': 1})
            chosen = await start(f, actor=2, roles=['PHOTOGRAPHER', extra_role],
                guest={'name': 'Гость', 'hotelId': 1, 'photographerId': 5})
            async with f.factory() as session:
                assert (await session.get(Booking, chosen['bookingId'])).photographer_id == 5
            response = await f.client.get('/api/miniapp/workflow',
                headers={'X-Telegram-Init-Data': signed(2)})
            listing = await response.json()
            row = next(b for b in listing['bookings'] if b['id'] == chosen['bookingId'])
            assert row['photographerId'] == 5 and row['photographer'] == 'Other real photographer'
        finally:
            await close(f)
    asyncio.run(run())
