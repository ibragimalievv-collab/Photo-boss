import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from test_team import Request
from test_workflow_replay import ACTOR, packet, setup
from test_workflow_services import booking_data, fixture

from app.models import Hotel, HotelEmployee, Notification, ShiftCheckIn, User, UserRole
from app.push_notifications import AppPush
from app.services.bookings import create_booking_record, notify_manager_ready_for_sale


def test_ready_notice_has_hotel_guest_and_scoped_recipients_without_phone_permission(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    async def run():
        engine, factory = await fixture()
        service = AppPush(SimpleNamespace(engine=engine))
        try:
            async with factory() as session:
                session.add_all([User(id=3, tg_id=3, name='Owner'), UserRole(user_id=3, role='OWNER'),
                    User(id=4, tg_id=4, name='Hotel admin'), UserRole(user_id=4, role='ADMIN'),
                    HotelEmployee(user_id=4, hotel_id=1), Hotel(id=2, name='Other hotel'),
                    User(id=5, tg_id=5, name='Other admin'), UserRole(user_id=5, role='ADMIN'),
                    HotelEmployee(user_id=5, hotel_id=2),
                    User(id=6, tg_id=6, name='Unrelated manager'), UserRole(user_id=6, role='MANAGER')])
                (await session.get(Hotel, 1)).name = 'Мовенпик'
                booking = await create_booking_record(session, await session.get(User, 1),
                    booking_data() | {'client_name': 'Сигизмунд'})
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                await notify_manager_ready_for_sale(AsyncMock(), session, booking)
                await notify_manager_ready_for_sale(AsyncMock(), session, booking)
                await session.commit()
                notices = list(await session.scalars(select(Notification).where(Notification.kind == 'sale_ready')))
                assert len(notices) == 4 and {n.user_id for n in notices} == {1, 2, 3, 4}
                session.add(Notification(user_id=1, kind='sale_ready', event_key='legacy-ready:1',
                    text=f'Съёмка №{booking.id} готова к продаже.'))
                session.add(Notification(user_id=6, kind='sale_ready', event_key=f'sale-ready:{booking.id}',
                    text='Should not reveal another booking'))
                await session.commit()
            for uid in (1, 2, 3, 4):
                feed = json.loads((await service.status(Request({'id': uid}, {}))).text)
                assert feed['activeDevices'] == 0
                assert feed['items']
                for item in feed['items']:
                    assert 'Съёмка готова к продаже' in item['title']
                    assert 'Отель: Мовенпик' in item['description']
                    assert 'Гость: Сигизмунд' in item['description']
                    assert 'Съёмка №' not in item['description']
                    assert item['url'].endswith(f'?booking={booking.id}')
            for uid in (5, 6):
                assert not json.loads((await service.status(Request({'id': uid}, {}))).text)['items']
            async with factory() as session:
                await session.delete(await session.scalar(select(HotelEmployee).where(HotelEmployee.user_id == 4)))
                await session.commit()
            assert not json.loads((await service.status(Request({'id': 4}, {}))).text)['items']
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_lunch_start_return_notify_once_in_app_and_recheck_current_hotel_scope(monkeypatch):
    moment = datetime(2026, 9, 22, 10, tzinfo=UTC).replace(tzinfo=None)
    monkeypatch.setattr('app.work_breaks.utc_now', lambda: moment)
    async def run():
        engine, factory, work = await setup()
        service = AppPush(SimpleNamespace(engine=engine))
        try:
            async with factory() as session:
                session.add_all([HotelEmployee(hotel_id=1, user_id=1),
                    ShiftCheckIn(user_id=1, shift_date=date(2026, 9, 22), status='STARTED', started_at=moment),
                    User(id=3, tg_id=3, name='Owner'), UserRole(user_id=3, role='OWNER'),
                    User(id=4, tg_id=4, name='Admin'), UserRole(user_id=4, role='ADMIN'),
                    HotelEmployee(user_id=4, hotel_id=1)])
                await session.commit()
            for key, kind in [('lunch-notice', 'break_start'), ('return-notice', 'break_end')]:
                body = packet(key, kind, {'hotelId': 1})
                first = json.loads((await work.execute(ACTOR, body)).text)
                assert first == json.loads((await work.execute(ACTOR, body)).text)
            async with factory() as session:
                notices = list(await session.scalars(select(Notification)))
                assert len(notices) == 6
                assert {n.user_id for n in notices} == {1, 3, 4}
            for uid in (1, 3, 4):
                feed = json.loads((await service.status(Request({'id': uid}, {}))).text)
                assert len(feed['items']) == 2 and feed['activeDevices'] == 0
                assert any('ушёл на обед' in n['title'] for n in feed['items'])
                assert any('вернулся с обеда' in n['title'] for n in feed['items'])
                assert all('Сотрудник: Manager' in n['description'] and 'Отель: Hotel' in n['description'] for n in feed['items'])
            assert not json.loads((await service.status(Request({'id': 2}, {}))).text)['items']
            async with factory() as session:
                await session.delete(await session.scalar(select(HotelEmployee).where(HotelEmployee.user_id == 4)))
                await session.commit()
            assert not json.loads((await service.status(Request({'id': 4}, {}))).text)['items']
        finally:
            await engine.dispose()
    asyncio.run(run())
