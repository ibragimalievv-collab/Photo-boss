import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import select
from test_workflow_services import fixture

from app.models import Notification, OwnerTelegramDelivery, User, UserRole
from app.owner_telegram import deliver_owner_copies
from app.push_notifications import queue_notice


def test_owner_copy_preserves_app_read_state_and_never_sends_staff_or_history(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    monkeypatch.setattr('app.owner_telegram.config', SimpleNamespace(admin_ids=(3,)))
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
