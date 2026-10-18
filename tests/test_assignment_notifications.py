"""Committed assignments survive Telegram failures without notifying stale staff."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_workflow_services import booking_data, fixture

from app.models import Booking, Notification, Setting, User, UserRole, utc_now
from app.services.assignment_notifications import (
    assignment_items,
    deliver_assignments,
    queue_assignment,
)
from app.services.bookings import create_booking_record


async def seed():
    engine, factory = await fixture()
    async with factory() as session:
        session.add_all([User(id=3, tg_id=3003, name='Second photographer'),
                         UserRole(user_id=3, role='PHOTOGRAPHER')])
        actor = await session.get(User, 1)
        data = booking_data() | {'shoot_date': str(utc_now().date()+timedelta(days=1))}
        booking = await create_booking_record(session, actor, data)
        booking.photographer_id, booking.status = 2, 'ASSIGNED'
        await session.flush()
        await queue_assignment(await session.connection(), booking.id, 2)
        await session.commit()
        bid = booking.id
    return engine, factory, bid


def test_assignment_rollback_replay_and_reassignment_are_atomic():
    async def run():
        engine, factory, bid = await seed()
        try:
            async with factory() as session:
                await queue_assignment(await session.connection(), bid, 2)
                await session.commit()
                assert await session.scalar(select(func.count(Notification.id))) == 1
                booking = await session.get(Booking, bid)
                booking.photographer_id = 3
                await session.flush()
                await queue_assignment(await session.connection(), bid, 3)
                await session.rollback()
            async with factory() as session:
                assert (await session.get(Booking, bid)).photographer_id == 2
                assert await session.scalar(select(func.count(Notification.id))) == 1
                for uid in [3, 2]:
                    (await session.get(Booking, bid)).photographer_id = uid
                    await session.flush()
                    await queue_assignment(await session.connection(), bid, uid)
                    await session.commit()
                notices = list(await session.scalars(select(Notification).order_by(Notification.id)))
                assert [n.user_id for n in notices] == [2, 3, 2]
                assert [n.resolved_at is None for n in notices] == [False, False, True]
            bot = SimpleNamespace(send_message=AsyncMock())
            assert await deliver_assignments(engine, bot) == 1
            assert await deliver_assignments(engine, bot) == 0
            assert bot.send_message.await_count == 1
            call = bot.send_message.await_args
            assert call.args[0] == 2 and 'Hotel' in call.args[1] and '12:00' in call.args[1]
            assert call.kwargs['reply_markup'].inline_keyboard[0][0].web_app.url.endswith(f'#shootings?booking={bid}')
            async with engine.connect() as conn:
                assert len(await assignment_items(conn, 2, utc_now().date())) == 1
                assert await assignment_items(conn, 3, utc_now().date()) == []
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('stale', ['reassigned', 'cancelled', 'inactive', 'role_removed', 'trashed'])
def test_failed_delivery_retries_but_stale_assignments_are_never_sent(stale, monkeypatch):
    async def run():
        engine, factory, bid = await seed()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=OSError('unavailable')))
        try:
            assert await deliver_assignments(engine, bot) == 0
            async with factory() as session:
                notice = await session.scalar(select(Notification))
                assert not notice.sent and notice.resolved_at is None
                booking = await session.get(Booking, bid)
                if stale == 'reassigned':
                    booking.photographer_id = 3
                elif stale == 'cancelled':
                    booking.status = 'CANCELLED'
                elif stale == 'inactive':
                    (await session.get(User, 2)).active = False
                elif stale == 'role_removed':
                    await session.delete(await session.scalar(select(UserRole).where(UserRole.user_id == 2)))
                else:
                    session.add(Setting(key=f'booking:trash:{bid}', value='{}'))
                await session.commit()
            bot.send_message.reset_mock(side_effect=True)
            from app.services import assignment_notifications
            now = utc_now()+timedelta(minutes=2)
            monkeypatch.setattr(assignment_notifications, 'utc_now', lambda: now)
            assert await deliver_assignments(engine, bot) == 0
            bot.send_message.assert_not_awaited()
            async with factory() as session:
                assert (await session.scalar(select(Notification))).resolved_at is not None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_failed_telegram_delivery_is_retried_after_recovery(monkeypatch):
    async def run():
        engine, _factory, _bid = await seed()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=OSError('unavailable')))
        try:
            assert await deliver_assignments(engine, bot) == 0
            bot.send_message.reset_mock(side_effect=True)
            assert await deliver_assignments(engine, bot) == 0
            bot.send_message.assert_not_awaited()
            from app.services import assignment_notifications
            now = utc_now()+timedelta(minutes=2)
            monkeypatch.setattr(assignment_notifications, 'utc_now', lambda: now)
            assert await deliver_assignments(engine, bot) == 1
            assert await deliver_assignments(engine, bot) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_backed_off_first_page_does_not_starve_later_eligible_assignments():
    async def run():
        engine, factory, _bid = await seed()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                for number in range(103):
                    booking = await create_booking_record(session, actor, booking_data() |
                        {'room': str(200+number), 'shoot_date': str(utc_now().date()+timedelta(days=1))})
                    booking.photographer_id, booking.status = 2, 'ASSIGNED'
                    await session.flush()
                    await queue_assignment(await session.connection(), booking.id, 2)
                notices = list(await session.scalars(select(Notification).order_by(Notification.id)))
                for notice in notices[:100]:
                    payload = json.loads(notice.payload)
                    payload['nextAttemptAt'] = (utc_now()+timedelta(minutes=10)).isoformat()
                    notice.payload = json.dumps(payload)
                await session.commit()
                eligible = [n.id for n in notices[100:]]
            bot = SimpleNamespace(send_message=AsyncMock())
            assert await deliver_assignments(engine, bot, limit=3) == 3
            assert bot.send_message.await_count == 3
            async with factory() as session:
                sent = list(await session.scalars(select(Notification.id).where(Notification.sent.is_(True))))
                assert sent == eligible[:3]
            # The remaining due notice is reached on the next invocation.
            assert await deliver_assignments(engine, bot, limit=3) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_assignments_arriving_during_delivery_wait_for_the_next_invocation(monkeypatch):
    async def run():
        engine, factory, _bid = await seed()
        injected = False
        try:
            class InjectingSession(AsyncSession):
                async def commit(self):
                    nonlocal injected
                    await super().commit()
                    if injected:
                        return
                    injected = True
                    # A new assignment commits after the invocation's first send.
                    async with factory() as another:
                        actor = await another.get(User, 1)
                        booking = await create_booking_record(another, actor, booking_data() |
                            {'room': 'new arrival', 'shoot_date': str(utc_now().date()+timedelta(days=1))})
                        booking.photographer_id, booking.status = 2, 'ASSIGNED'
                        await another.flush()
                        await queue_assignment(await another.connection(), booking.id, 2)
                        await another.commit()
            from app.services import assignment_notifications
            monkeypatch.setattr(assignment_notifications, 'AsyncSession', InjectingSession)
            bot = SimpleNamespace(send_message=AsyncMock())
            assert await deliver_assignments(engine, bot, limit=3) == 1
            assert bot.send_message.await_count == 1
            assert await deliver_assignments(engine, bot, limit=3) == 1
            assert bot.send_message.await_count == 2
        finally:
            await engine.dispose()
    asyncio.run(run())
