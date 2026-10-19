import asyncio
import base64
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select
from test_team import Request
from test_workflow_services import fixture

from app.miniapp_security import AccessError
from app.models import Notification, PushDelivery, PushSubscription, utc_now
from app.push_notifications import (
    AppPush,
    deliver_push,
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
