"""Optional owner delivery, reusing Notification and owner settings.

Only a daily summary and batched critical findings can be sent. Telegram has
no send-message idempotency key: a crash after acceptance but before commit
can repeat a delivery; financial records are never modified here.
"""
import asyncio
from datetime import timedelta
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from ..models import Notification, Setting, User, UserRole
from .discipline import utc
from .insights import parsed


async def deliver_owner_notifications(session, bot, now, tz_name):
    day = now.astimezone(ZoneInfo(tz_name)).date()-timedelta(days=1)
    owner_ids = list(await session.scalars(select(UserRole.user_id).where(UserRole.role=='OWNER').order_by(UserRole.user_id)))
    sent = 0
    for uid in owner_ids:
        user = await session.get(User,uid,with_for_update=True)
        if not user or not user.active or not await session.scalar(select(UserRole.id).where(UserRole.user_id==uid,UserRole.role=='OWNER')):
            continue
        setting = await session.get(Setting,f'notify:owner:{uid}')
        prefs = parsed(setting.value) if setting else {}
        if prefs.get('daily'):
            summary = await session.scalar(select(Notification).where(Notification.user_id==uid,
                Notification.kind=='daily_summary',Notification.event_key==f'daily:{day}',Notification.sent.is_(False)))
            if summary:
                try:
                    async with asyncio.timeout(15):
                        await bot.send_message(user.tg_id,summary.text)
                except (TelegramAPIError,TimeoutError):
                    continue
                summary.sent=True
                sent += 1
        if not prefs.get('critical'):
            continue
        throttle = await session.get(Setting,f'notify:last-critical:{uid}')
        if throttle:
            try:
                if now-utc(throttle.value) < timedelta(hours=1):
                    continue
            except ValueError:
                pass
        alerts = list(await session.scalars(select(Notification).where(Notification.user_id==uid,
            Notification.kind=='control',Notification.priority=='critical',Notification.sent.is_(False),
            Notification.resolved_at.is_(None),Notification.acknowledged_at.is_(None)).order_by(Notification.id).limit(12)))
        if not alerts:
            continue
        message = 'Photo Boss · требуется проверка\n'+'\n'.join(f'• {n.text} · событие №{n.id}' for n in alerts)
        message += '\nПричины и исходные данные — в центре событий. Автоматических списаний и исправлений нет.'
        try:
            async with asyncio.timeout(15):
                await bot.send_message(user.tg_id,message)
        except (TelegramAPIError,TimeoutError):
            continue
        for notification in alerts:
            notification.sent=True
        if throttle:
            throttle.value=now.isoformat()
        else:
            session.add(Setting(key=f'notify:last-critical:{uid}',value=now.isoformat()))
        sent += 1
    await session.flush()
    return sent
