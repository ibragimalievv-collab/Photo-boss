"""Additional owner-only Telegram copies; never consumes in-app read state.

Telegram has no send idempotency key: a process loss after provider acceptance
and before committing can repeat a copy. No historical catch-up on activation.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import config
from .models import (
    Notification,
    OwnerTelegramDelivery,
    Setting,
    User,
    UserRole,
    utc_now,
)
from .push_notifications import app_channel, eligible, payload_for

log = logging.getLogger(__name__)
START_KEY = 'notify:owner-telegram:enabled-at'


async def deliver_owner_copies(engine, bot):
    if not app_channel():
        return 0
    async with AsyncSession(engine) as session:
        start = await session.get(Setting, START_KEY)
        if not start:
            session.add(Setting(key=START_KEY, value=utc_now().isoformat()))
            await session.commit()
            return 0
        since = max(datetime.fromisoformat(start.value), utc_now()-timedelta(days=1))
        notices = list(await session.scalars(select(Notification).join(User, User.id == Notification.user_id)
            .where(User.active.is_(True), User.tg_id.in_(config.admin_ids),
                User.id.in_(select(UserRole.user_id).where(UserRole.role == 'OWNER')),
                Notification.created_at >= since, Notification.resolved_at.is_(None),
                ~select(OwnerTelegramDelivery.notification_id).where(
                    OwnerTelegramDelivery.notification_id == Notification.id).exists())
            .order_by(Notification.id).limit(100)))
        for notice in notices:
            session.add(OwnerTelegramDelivery(notification_id=notice.id))
        await session.commit()
        ids = list(await session.scalars(select(OwnerTelegramDelivery.notification_id).where(
            OwnerTelegramDelivery.status == 'PENDING', OwnerTelegramDelivery.next_attempt_at <= utc_now())
            .order_by(OwnerTelegramDelivery.notification_id).limit(20)))
    accepted = 0
    for nid in ids:
        async with AsyncSession(engine) as session:
            delivery = await session.scalar(select(OwnerTelegramDelivery).where(
                OwnerTelegramDelivery.notification_id == nid).with_for_update(skip_locked=True))
            if not delivery or delivery.status != 'PENDING':
                continue
            notice = await session.get(Notification, nid)
            user = await session.get(User, notice.user_id) if notice else None
            owner = user and await session.scalar(select(UserRole.id).where(
                UserRole.user_id == user.id, UserRole.role == 'OWNER'))
            if (not owner or not user.active or user.tg_id not in config.admin_ids or notice.created_at < since
                    or not await eligible(session, notice, SimpleNamespace(user_id=user.id, active=True),
                        include_read=True, for_delivery=True)):
                delivery.status = 'SKIPPED'
            else:
                delivery.attempts += 1
                payload = payload_for(notice)
                # Only a relative application URL from the server's allowlisted builder.
                path = payload['url'] if payload['url'].startswith('/app/#') else '/app/#home'
                try:
                    async with asyncio.timeout(15):
                        await bot.send_message(user.tg_id,
                            payload['title']+'\n'+notice.text[:3200]+'\nhttps://photo-boss.onrender.com'+path,
                            parse_mode=None)
                except TelegramForbiddenError:
                    delivery.status, delivery.last_error = 'FAILED', 'bot_blocked'
                except (TelegramAPIError, TimeoutError, OSError) as exc:
                    delivery.last_error = type(exc).__name__[:40]
                    delivery.status = 'FAILED' if delivery.attempts >= 8 else 'PENDING'
                    delivery.next_attempt_at = utc_now()+timedelta(seconds=min(1800, 15*2**delivery.attempts))
                else:
                    delivery.status, delivery.accepted_at, delivery.last_error = 'ACCEPTED', utc_now(), None
                    accepted += 1
            await session.commit()
    return accepted


async def owner_telegram_loop(engine, bot):
    from .operations_guard import operation_lease
    while True:
        try:
            async with operation_lease(engine):
                await deliver_owner_copies(engine, bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable queue; no message/identifier logging
            log.warning('Owner Telegram delivery deferred (%s)', type(exc).__name__)
        await asyncio.sleep(5)
