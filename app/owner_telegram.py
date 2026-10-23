"""Additional owner-only Telegram copies; never consumes in-app read state.

Telegram has no send idempotency key: a process loss after provider acceptance
and before committing can repeat a copy. No historical catch-up on activation.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import config
from .models import (
    AuditLog,
    Notification,
    OwnerTelegramDelivery,
    Setting,
    User,
    UserRole,
    utc_now,
)
from .push_notifications import app_channel, eligible, payload_for, queue_notice

log = logging.getLogger(__name__)
START_KEY = 'notify:owner-telegram:enabled-at'
OWNER_KEY = 'notify:owner-telegram:'
WORK_EVENTS = {
    'booking_created': 'Создана запись', 'booking_updated': 'Запись изменена',
    'booking_rescheduled': 'Съёмка перенесена', 'booking_cancelled': 'Съёмка отменена',
    'booking_trashed': 'Запись перемещена в корзину', 'booking_restored': 'Запись восстановлена',
    'receipt_uploaded': 'Загружен чек предоплаты', 'sale_receipt_captured': 'Загружен чек продажи',
    'shift_report_saved': 'Сохранён отчёт смены', 'break_corrected': 'Исправлена отметка обеда',
    'hotel_tariff_saved': 'Изменены цены отеля', 'candidate_created': 'Новый кандидат',
    'candidate_updated': 'Кандидат обновлён', 'candidate_hired': 'Кандидат принят',
    'miniapp_employee_updated': 'Изменены данные сотрудника',
    'miniapp_owner_access_changed': 'Изменён доступ владельца',
    'delivery_handoff': 'Фотографии переданы гостю',
    'full_shoot_upload_completed': 'Завершена загрузка съёмки',
    'shoot_storage_recovery': 'Запущено восстановление фотографий',
}


async def queue_owner_work_events(engine):
    """Committed audit events only; never copies private audit detail strings."""
    async with AsyncSession(engine) as session:
        start = await session.get(Setting, START_KEY)
        if not start:
            return
        since = max(datetime.fromisoformat(start.value), utc_now()-timedelta(days=1))
        owners = list(await session.scalars(select(User.id).where(User.active.is_(True),
            User.id.in_(select(UserRole.user_id).where(UserRole.role == 'OWNER')))))
        for uid in owners:
            events = list(await session.scalars(select(AuditLog).where(
                AuditLog.created_at >= since, AuditLog.action.in_(WORK_EVENTS),
                ~select(Notification.id).where(Notification.user_id == uid,
                    Notification.event_key == 'owner-audit:' + AuditLog.id.cast(String)).exists())
                .order_by(AuditLog.id).limit(100)))
            for event in events:
                route = ('/app/#shootings?booking='+str(event.entity_id) if event.entity == 'booking'
                         else '/app/#workflow' if event.entity in {'receipt', 'sale_draft', 'shooting'}
                         else '/app/#workday' if event.entity in {'shift_check_out', 'work_break'}
                         else '/app/#team' if event.entity in {'hr_candidate', 'user'} else '/app/#audit')
                await queue_notice(session, uid, kind='work_event', event_key=f'owner-audit:{event.id}',
                    message=WORK_EVENTS[event.action]+f'. Событие №{event.id}; объект №{event.entity_id or 0}.',
                    url=route)
        await session.commit()


async def queue_connection_state(engine, storage):
    from .bank_payments import configured as bank_configured
    from .push_notifications import configured as push_configured
    from .work_calls import ice_config
    state = storage.public_status()
    checks = {
        'Яндекс.Диск: подключение, чтение и запись': all(state.get(k) for k in
            ('configured', 'connected', 'readVerified', 'writeVerified')),
        'Системные push: настройки сервера': push_configured(),
        'Банк: настройки эквайринга': bank_configured(),
        'Звонки: TURN для сложных сетей': ice_config(0)['relayConfigured'],
        'ИИ: ключ провайдера настроен': bool(config.openai_api_key),
    }
    async with AsyncSession(engine) as session:
        key = 'notify:connection-state'
        saved = await session.get(Setting, key, with_for_update=True)
        previous = json.loads(saved.value) if saved else {}
        if previous.get('checks') == checks:
            return
        version = previous.get('version', 0)+1
        text = 'Состояние подключений Photo Boss:\n'+'\n'.join(
            f'{name}: '+('подтверждено' if good else 'не подтверждено / не настроено')
            for name, good in checks.items())
        text += '\nНастройки ИИ, push и банка не доказывают успешную операцию на устройстве или у провайдера.'
        owners = list(await session.scalars(select(User.id).where(User.active.is_(True),
            User.id.in_(select(UserRole.user_id).where(UserRole.role == 'OWNER')))))
        for uid in owners:
            await queue_notice(session, uid, kind='service_status',
                event_key=f'connection-state:{version}', message=text, url='/app/#more')
        value = json.dumps({'version': version, 'checks': checks}, ensure_ascii=False)
        if saved:
            saved.value = value
        else:
            session.add(Setting(key=key, value=value))
        await session.commit()


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
            .where(User.active.is_(True), User.tg_id > 0,
                User.id.in_(select(UserRole.user_id).where(UserRole.role == 'OWNER')),
                select(Setting.key).where(Setting.key == OWNER_KEY + User.id.cast(String)).exists(),
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
            preference = await session.get(Setting, OWNER_KEY+str(user.id)) if user else None
            if (not owner or not user.active or user.tg_id <= 0 or not preference
                    or notice.created_at < max(since, datetime.fromisoformat(preference.value))
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


async def owner_telegram_loop(engine, bot, storage=None):
    from .operations_guard import operation_lease
    while True:
        try:
            async with operation_lease(engine):
                await deliver_owner_copies(engine, bot)
                await queue_owner_work_events(engine)
                if storage is not None:
                    await queue_connection_state(engine, storage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable queue; no message/identifier logging
            log.warning('Owner Telegram delivery deferred (%s)', type(exc).__name__)
        await asyncio.sleep(5)
