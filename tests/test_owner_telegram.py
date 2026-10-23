import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import select
from test_workflow_services import fixture

from app.miniapp_security import AccessError
from app.models import (
    AuditLog,
    Notification,
    OwnerTelegramDelivery,
    Setting,
    User,
    UserRole,
    utc_now,
)
from app.owner_telegram import (
    OWNER_KEY,
    deliver_owner_copies,
    queue_connection_state,
    queue_owner_work_events,
)
from app.push_notifications import AppPush, queue_notice


def test_only_current_owner_can_enable_own_telegram_copies(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    async def run():
        engine, factory = await fixture()
        api = SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda request: request['body']))
        service = AppPush(api)
        try:
            async with factory() as session:
                session.add(User(id=3, tg_id=3, name='Isolated owner'))
                await session.flush()
                session.add(UserRole(user_id=3, role='OWNER'))
                await session.commit()
            def request(uid, body):
                return {'miniapp_actor': {'id': uid}, 'body': body}
            for uid in (1, 2):
                with pytest.raises(AccessError) as denied:
                    await service.owner_telegram(request(uid, {'enabled': True}))
                assert denied.value.status == 403
            with pytest.raises(AccessError) as invalid:
                await service.owner_telegram(request(3, {'enabled': True, 'userId': 1}))
            assert invalid.value.status == 400
            await service.owner_telegram(request(3, {'enabled': True}))
            await service.owner_telegram(request(3, {'enabled': True}))
            assert json.loads((await service.status(request(3, {}))).text)['ownerTelegram']
            await service.owner_telegram(request(3, {'enabled': False}))
            async with factory() as session:
                assert await session.get(Setting, OWNER_KEY+'3') is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_owner_copy_preserves_app_read_state_and_never_sends_staff_or_history(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    monkeypatch.setattr('app.owner_telegram.config', SimpleNamespace(admin_ids=(3,), openai_api_key=''))
    async def run():
        engine, factory = await fixture()
        bot = SimpleNamespace(send_message=AsyncMock())
        try:
            async with factory() as session:
                session.add(User(id=3, tg_id=3, name='Isolated owner'))
                await session.flush()
                session.add(UserRole(user_id=3, role='OWNER'))
                await queue_notice(session, 3, kind='chat', event_key='historical')
                await session.commit()
            assert await deliver_owner_copies(engine, bot) == 0
            async with factory() as session:
                session.add(Setting(key=OWNER_KEY+'3', value=utc_now().isoformat()))
                nid = await queue_notice(session, 3, kind='chat', event_key='fresh')
                await queue_notice(session, 1, kind='chat', event_key='staff-private')
                await session.commit()
            assert await deliver_owner_copies(engine, bot) == 1
            assert await deliver_owner_copies(engine, bot) == 0
            assert bot.send_message.await_count == 1
            assert bot.send_message.call_args.args[0] == 3
            async with factory() as session:
                notice = await session.get(Notification, nid)
                assert notice.acknowledged_at is None and not notice.sent
                assert (await session.get(OwnerTelegramDelivery, nid)).status == 'ACCEPTED'
                assert len(list(await session.scalars(select(OwnerTelegramDelivery)))) == 1
                session.add(AuditLog(action='booking_created', entity='booking', entity_id=42,
                    details='Private audit details must never be copied'))
                await session.commit()
            await queue_owner_work_events(engine)
            await queue_owner_work_events(engine)
            async with factory() as session:
                events = list(await session.scalars(select(Notification).where(Notification.kind == 'work_event')))
                assert len(events) == 1 and events[0].user_id == 3
                assert 'Private audit' not in events[0].text
                assert 'booking=42' in events[0].payload
            storage = SimpleNamespace(public_status=lambda: {'configured': True, 'connected': True,
                'readVerified': True, 'writeVerified': True})
            await queue_connection_state(engine, storage)
            await queue_connection_state(engine, storage)
            async with factory() as session:
                assert len(list(await session.scalars(select(Notification).where(
                    Notification.kind == 'service_status')))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_blocked_bot_is_visible_failed_without_losing_in_app_event(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    monkeypatch.setattr('app.owner_telegram.config', SimpleNamespace(admin_ids=(3,)))
    async def run():
        engine, factory = await fixture()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramForbiddenError(
            method=SendMessage(chat_id=3, text='CI only'), message='blocked')))
        try:
            async with factory() as session:
                session.add(User(id=3, tg_id=3, name='Isolated owner'))
                await session.flush()
                session.add(UserRole(user_id=3, role='OWNER'))
                await session.commit()
            await deliver_owner_copies(engine, bot)
            async with factory() as session:
                session.add(Setting(key=OWNER_KEY+'3', value=utc_now().isoformat()))
                nid = await queue_notice(session, 3, kind='chat', event_key='fresh')
                await session.commit()
            assert await deliver_owner_copies(engine, bot) == 0
            assert await deliver_owner_copies(engine, bot) == 0
            async with factory() as session:
                assert (await session.get(OwnerTelegramDelivery, nid)).last_error == 'bot_blocked'
                assert (await session.get(Notification, nid)).acknowledged_at is None
            assert bot.send_message.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
