"""Booking control respects app-only routing and current hotel visibility."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import delete, func, select
from test_team import Request
from test_workflow_services import fixture

from app import booking_control, push_notifications
from app.models import (
    Hotel,
    HotelEmployee,
    Notification,
    PushDelivery,
    PushSubscription,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
)
from app.push_notifications import AppPush, deliver_push, eligible, payload_for

NOW = datetime(2026, 9, 22, 8, tzinfo=UTC)


def app_mode(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    for key in ('WEB_PUSH_PUBLIC_KEY', 'WEB_PUSH_PRIVATE_KEY', 'WEB_PUSH_SUBJECT'):
        monkeypatch.setenv(key, 'test-only')
    monkeypatch.setattr(booking_control, 'config', SimpleNamespace(training_timezone='Europe/Moscow', openai_api_key=None))
    activity = booking_control.activity

    async def current_activity(session, **kwargs):
        kwargs.setdefault('now', NOW)
        return await activity(session, **kwargs)
    monkeypatch.setattr(booking_control, 'activity', current_activity)
    monkeypatch.setattr(push_notifications, 'utc_now', lambda: NOW.replace(tzinfo=None))


def test_app_channel_queues_manager_and_scoped_advice_once_without_telegram(monkeypatch):
    app_mode(monkeypatch)
    sender = AsyncMock(return_value=201)
    monkeypatch.setattr(push_notifications, 'send_push', sender)

    async def run():
        engine, factory = await fixture()
        bot = SimpleNamespace(send_message=AsyncMock())
        try:
            async with factory() as session:
                session.add_all([
                    User(id=3, tg_id=3, name='Owner'), UserRole(user_id=3, role='OWNER'),
                    User(id=4, tg_id=4, name='Admin'), UserRole(user_id=4, role='ADMIN'),
                    HotelEmployee(user_id=4, hotel_id=1),
                    Shift(user_id=1, hotel_id=1, start_at=NOW.replace(tzinfo=None)-timedelta(hours=2),
                        end_at=NOW.replace(tzinfo=None)+timedelta(hours=3)),
                    ShiftCheckIn(user_id=1, shift_date=NOW.date(), status='STARTED',
                        started_at=NOW.replace(tzinfo=None)-timedelta(hours=2)),
                    PushSubscription(user_id=1, endpoint_hash='test-device', subscription='{}',
                        created_at=NOW.replace(tzinfo=None)-timedelta(minutes=1)),
                ])
                await session.commit()
                assert await booking_control.activity_tick(session, bot, NOW) == 3
                await session.commit()
                assert await booking_control.activity_tick(session, bot, NOW) == 0
                assert await booking_control.activity_tick(session, bot, NOW+timedelta(minutes=59)) == 0
                await session.commit()
                assert await session.scalar(select(func.count(Notification.id))) == 3
                assert not any(await session.scalars(select(Notification.sent)))
            bot.send_message.assert_not_awaited()
            feed = AppPush(SimpleNamespace(engine=engine))
            manager = json.loads((await feed.status(Request({'id': 1}))).text)
            admin = json.loads((await feed.status(Request({'id': 4}))).text)
            assert [item['kind'] for item in manager['items']] == ['manager_idle']
            assert [item['kind'] for item in admin['items']] == ['manager_advice']
            assert all(item['url'] == '/app/#home' for item in manager['items']+admin['items'])
            # The isolated reminder clock predates the model's real queue default.
            await deliver_push(engine)
            async with factory() as session:
                pending = await session.scalar(select(PushDelivery))
                pending.next_attempt_at = NOW.replace(tzinfo=None)
                await session.commit()
            assert await deliver_push(engine) == 1
            assert await deliver_push(engine) == 0
            assert sender.await_count == 1
            assert 'Manager' not in json.dumps(sender.await_args.args[1])
            async with factory() as session:
                assert await session.scalar(select(func.count(PushDelivery.id))) == 1
                assert await booking_control.activity_tick(session, bot, NOW+timedelta(minutes=60)) == 3
                await session.commit()
                assert await session.scalar(select(func.count(Notification.id))) == 6
                assert await session.scalar(select(func.count(Notification.id)).where(Notification.resolved_at.is_(None))) == 3
            bot.send_message.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_current_roles_scope_settings_and_shift_gate_manager_notices(monkeypatch):
    app_mode(monkeypatch)

    async def run():
        engine, factory = await fixture()
        bot = SimpleNamespace(send_message=AsyncMock())
        try:
            async with factory() as session:
                session.add_all([
                    Hotel(id=2, name='Other hotel'),
                    User(id=4, tg_id=4, name='Admin'), UserRole(user_id=4, role='ADMIN'),
                    HotelEmployee(user_id=4, hotel_id=1), HotelEmployee(user_id=4, hotel_id=2),
                    Shift(user_id=1, hotel_id=1, start_at=NOW.replace(tzinfo=None)-timedelta(hours=2),
                        end_at=NOW.replace(tzinfo=None)+timedelta(hours=3)),
                    ShiftCheckIn(user_id=1, shift_date=NOW.date(), status='STARTED',
                        started_at=NOW.replace(tzinfo=None)-timedelta(hours=2)),
                ])
                await session.commit()
                await booking_control.activity_tick(session, bot, NOW)
                await session.commit()
                advice = await session.scalar(select(Notification).where(Notification.kind == 'manager_advice'))
                manager = await session.scalar(select(Notification).where(Notification.kind == 'manager_idle'))
                sub = SimpleNamespace(active=True, user_id=4)
                assert await eligible(session, advice, sub)
                assert json.loads(advice.payload)['hotelIds'] == [1, 2]
                await session.execute(delete(HotelEmployee).where(HotelEmployee.user_id == 4, HotelEmployee.hotel_id == 2))
                await session.flush()
                assert not await eligible(session, advice, sub), 'Old multi-hotel text must disappear after a scope loss'
                assert not await eligible(session, advice, sub, for_delivery=True)
                sub.user_id = 1
                assert await eligible(session, manager, sub)
                session.add(ShiftCheckOut(user_id=1, shift_date=NOW.date(), status='FINISHED', ended_at=NOW.replace(tzinfo=None)))
                await session.flush()
                assert not await eligible(session, manager, sub)
                session.add(Setting(key='booking-control:settings', value='{"enabled": false}'))
                await session.flush()
                sub.user_id = 4
                assert not await eligible(session, advice, sub)
                assert 'Admin' not in json.dumps(payload_for(advice))
        finally:
            await engine.dispose()
    asyncio.run(run())
