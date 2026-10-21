import asyncio
import base64
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select
from test_team import Request
from test_workflow_services import fixture

from app.miniapp_security import AccessError
from app.models import (
    Hotel,
    HotelEmployee,
    Notification,
    PushDelivery,
    PushSubscription,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
    utc_now,
)
from app.push_notifications import (
    AppPush,
    deliver_push,
    eligible,
    payload_for,
    queue_notice,
    validate_subscription,
)
from app.services.assignment_notifications import deliver_assignments


def subscription():
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    enc = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    return {'endpoint': 'https://web.push.apple.com/test-device-endpoint',
            'keys': {'p256dh': enc(public), 'auth': enc(b'0123456789abcdef')}}


@pytest.fixture
def push_env(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    for key in ('WEB_PUSH_PUBLIC_KEY', 'WEB_PUSH_PRIVATE_KEY', 'WEB_PUSH_SUBJECT'):
        monkeypatch.setenv(key, 'test-only')


@pytest.mark.parametrize('endpoint', ['http://web.push.apple.com/path', 'https://127.0.0.1/private',
    'https://web.push.apple.com.evil.example/path', 'https://user:pass@web.push.apple.com/path',
    'https://web.push.apple.com:8443/path'])
def test_push_endpoint_cannot_target_private_or_untrusted_servers(endpoint):
    with pytest.raises(AccessError):
        validate_subscription(subscription() | {'endpoint': endpoint})


def test_push_retry_each_device_once_and_expired_subscription_is_removed(push_env, monkeypatch):
    sender = AsyncMock(return_value=503)
    monkeypatch.setattr('app.push_notifications.send_push', sender)
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                sub = PushSubscription(user_id=1, endpoint_hash='device', subscription=json.dumps(subscription()),
                    created_at=utc_now()-timedelta(seconds=5))
                session.add(sub)
                await queue_notice(session, 1, kind='chat', event_key='message:1', message='Private guest phone must never leave')
                await session.commit()
            assert await deliver_push(engine) == 0
            async with factory() as session:
                row = await session.scalar(select(PushDelivery))
                assert row.status == 'PENDING' and row.attempts == 1
                row.next_attempt_at = utc_now()-timedelta(seconds=1)
                await session.commit()
            sender.return_value = 201
            assert await deliver_push(engine) == 1
            assert await deliver_push(engine) == 0
            assert sender.call_count == 2
            assert 'Private guest' not in json.dumps(sender.call_args.args[1])
            async with factory() as session:
                assert await session.scalar(select(func.count(PushDelivery.id))) == 1
                await queue_notice(session, 1, kind='chat', event_key='message:2')
                await session.commit()
            sender.return_value = 410
            await deliver_push(engine)
            async with factory() as session:
                assert not (await session.scalar(select(PushSubscription))).active
                assert await session.scalar(select(func.count(Notification.id))) == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_switching_account_never_delivers_old_pending_notification(push_env, monkeypatch):
    sender = AsyncMock(return_value=503)
    monkeypatch.setattr('app.push_notifications.send_push', sender)
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                session.add(PushSubscription(user_id=1, endpoint_hash='device', subscription=json.dumps(subscription()),
                    created_at=utc_now()-timedelta(seconds=5)))
                await queue_notice(session, 1, kind='chat', event_key='private-message')
                await session.commit()
            await deliver_push(engine)
            async with factory() as session:
                sub = await session.scalar(select(PushSubscription))
                sub.user_id = 2
                row = await session.scalar(select(PushDelivery))
                row.next_attempt_at = utc_now()-timedelta(seconds=1)
                await session.commit()
            assert await deliver_push(engine) == 0
            assert sender.call_count == 1
            async with factory() as session:
                assert (await session.scalar(select(PushDelivery))).status == 'SKIPPED'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_notifications_list_is_private_and_request_role_cannot_grant_access(push_env):
    async def run():
        engine, factory = await fixture()
        try:
            from types import SimpleNamespace
            svc = AppPush(SimpleNamespace(engine=engine))
            async with factory() as session:
                await queue_notice(session, 1, kind='chat', event_key='one')
                await queue_notice(session, 2, kind='chat', event_key='two')
                await session.commit()
            result = json.loads((await svc.status(Request({'id': 1}, {}))).text)
            assert len(result['items']) == 1
            with pytest.raises(AccessError):
                await svc.status(Request({'id': 999, 'roles': ['OWNER']}, {}))
            bot = AsyncMock()
            assert await deliver_assignments(engine, bot) == 0
            from app.services.bookings import send_manager_ready_message
            await send_manager_ready_message(bot, 1, 34, engine=engine)
            bot.send_message.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_subscription_rebind_and_unsubscribe_are_bound_to_current_user(push_env):
    async def run():
        from types import SimpleNamespace
        engine, factory = await fixture()
        api = SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda r: r.body))
        svc, value = AppPush(api), subscription()
        try:
            await svc.subscribe(Request({'id': 1}, {'subscription': value}))
            await svc.subscribe(Request({'id': 2}, {'subscription': value}))
            await svc.unsubscribe(Request({'id': 1}, {'endpoint': value['endpoint']}))
            async with factory() as session:
                row = await session.scalar(select(PushSubscription))
                assert row.user_id == 2 and row.active
            await svc.unsubscribe(Request({'id': 2}, {'endpoint': value['endpoint']}))
            async with factory() as session:
                assert not (await session.scalar(select(PushSubscription))).active
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_new_device_does_not_report_historical_alert_as_waiting_for_delivery(push_env):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                await queue_notice(session, 1, kind='chat', event_key='old-message')
                await session.flush()
                notice = await session.scalar(select(Notification))
                session.add(PushSubscription(user_id=1, endpoint_hash='new-device',
                    subscription=json.dumps(subscription()), created_at=notice.created_at+timedelta(seconds=1)))
                await session.commit()
            service = AppPush(SimpleNamespace(engine=engine))
            feed = json.loads((await service.status(Request({'id': 1}, {}))).text)
            assert feed['items'][0]['pushState'] == 'HISTORY_ONLY'
            assert await deliver_push(engine) == 0
            async with factory() as session:
                assert await session.scalar(select(func.count(PushDelivery.id))) == 0
                await queue_notice(session, 1, kind='chat', event_key='new-message')
                # The fixture clock is real, so move the device timestamp back before the new alert.
                sub = await session.scalar(select(PushSubscription))
                sub.created_at = utc_now()-timedelta(seconds=2)
                await session.commit()
            feed = json.loads((await service.status(Request({'id': 1}, {}))).text)
            assert feed['items'][0]['pushState'] == 'PENDING'
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('purpose,model', [('start', ShiftCheckIn), ('end', ShiftCheckOut)])
def test_attendance_notifications_recheck_current_role_and_all_planned_hotel_scopes(push_env, purpose, model):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                session.add_all([User(id=3, tg_id=3, name='Owner'), User(id=4, tg_id=4, name='Admin'),
                    User(id=5, tg_id=5, name='Other admin'), UserRole(user_id=3, role='OWNER'),
                    UserRole(user_id=4, role='ADMIN'), UserRole(user_id=5, role='ADMIN'),
                    Hotel(id=2, name='Other'), HotelEmployee(user_id=4, hotel_id=1),
                    HotelEmployee(user_id=5, hotel_id=2)])
                row = model(user_id=2, shift_date=date(2026, 10, 21), status='PENDING_REVIEW')
                session.add(row)
                await session.flush()
                data = {'attendanceId': row.id, 'purpose': purpose, 'userId': 2, 'date': '2026-10-21'}
                for uid in (1, 2, 3, 4, 5):
                    await queue_notice(session, uid, kind='attendance', event_key=f'attendance:{purpose}:{row.id}',
                        payload=data, message='Личная отметка смены')
                await session.commit()
                notices = {n.user_id:n for n in await session.scalars(select(Notification))}
                async def allowed(uid):
                    return await eligible(session, notices[uid], SimpleNamespace(active=True, user_id=uid))
                assert not await allowed(1)  # An unrelated manager cannot read it.
                assert await allowed(2) and await allowed(3)
                assert not await allowed(4)  # Unplanned days need the owner.
                session.add(Shift(user_id=2, hotel_id=1,
                    start_at=datetime(2026,10,21,6,tzinfo=UTC).replace(tzinfo=None),
                    end_at=datetime(2026,10,21,18,tzinfo=UTC).replace(tzinfo=None)))
                await session.flush()
                assert await allowed(4) and not await allowed(5)
                session.add(Shift(user_id=2, hotel_id=2,
                    start_at=datetime(2026,10,21,14,tzinfo=UTC).replace(tzinfo=None),
                    end_at=datetime(2026,10,21,18,tzinfo=UTC).replace(tzinfo=None)))
                await session.flush()
                assert not await allowed(4)  # A shared day may expose another hotel's evidence.
                session.add(HotelEmployee(user_id=4, hotel_id=2))
                await session.flush()
                assert await allowed(4)
                phone = payload_for(notices[4])
                assert phone['url'] == '/app/#workday'
                assert ('начала' if purpose == 'start' else 'завершения') in phone['title']
                notices[4].payload = json.dumps(data | {'userId': 1})
                assert not await allowed(4)  # Payload cannot point to a different employee.
                await session.delete(row)
                await session.flush()
                assert not await allowed(2) and not await allowed(3)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_discount_notice_is_visible_only_to_requester_and_current_scoped_supervisors(push_env):
    async def run():
        from test_workflow_services import booking_data

        from app.models import SaleDiscountApproval
        from app.services.bookings import create_booking_record
        engine, factory = await fixture()
        try:
            async with factory() as session:
                session.add_all([User(id=3, tg_id=3, name='Owner'), User(id=4, tg_id=4, name='Admin'),
                    UserRole(user_id=3, role='OWNER'), UserRole(user_id=4, role='ADMIN'),
                    HotelEmployee(user_id=4, hotel_id=1)])
                booking = await create_booking_record(session, await session.get(User, 1), booking_data())
                booking.photographer_id = 2
                approval = SaleDiscountApproval(booking_id=booking.id, requested_by_id=2,
                    total_photos=150, unit_price=400)
                session.add(approval)
                await session.flush()
                for uid in (1, 2, 3, 4):
                    await queue_notice(session, uid, kind='sale_discount', event_key='discount-test',
                        payload={'bookingId': booking.id, 'discountId': approval.id})
                await session.flush()
                notices = {n.user_id:n for n in await session.scalars(select(Notification))}
                async def allowed(uid):
                    return await eligible(session, notices[uid], SimpleNamespace(active=True, user_id=uid))
                assert not await allowed(1)  # Related manager did not request this discount.
                assert await allowed(2) and await allowed(3) and await allowed(4)
                phone = payload_for(notices[3])
                assert phone['url'] == f'/app/#workflow?booking={booking.id}'
                assert 'скидки 50%' in phone['title']
                booking.photographer_id = None
                await session.flush()
                assert not await allowed(2)
                member = await session.scalar(select(HotelEmployee).where(HotelEmployee.user_id == 4))
                await session.delete(member)
                await session.flush()
                assert not await allowed(4) and await allowed(3)
                approval.booking_id = booking.id + 100
                assert not await allowed(3)
        finally:
            await engine.dispose()
    asyncio.run(run())
