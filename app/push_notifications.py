"""Account-bound Web Push. Provider acceptance is distinct from device delivery."""
import asyncio
import base64
import hashlib
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
from uuid import uuid4

from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import (
    Notification,
    PushDelivery,
    PushSubscription,
    Setting,
    User,
    UserRole,
    utc_now,
)
from .services.booking_notices import booking_description, notice_booking_id

log = logging.getLogger(__name__)
ROLES = {'OWNER', 'ADMIN', 'PHOTOGRAPHER', 'MANAGER'}
KINDS = {'service_status', 'work_event', 'booking_assignment', 'booking_reminder', 'control', 'daily_summary', 'chat', 'call', 'sale_ready', 'sale_created', 'academy', 'reminder', 'push_test', 'lunch_start', 'lunch_end', 'attendance', 'attendance_fine', 'sale_discount', 'manager_idle', 'manager_advice'}


def app_channel():
    return os.getenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'telegram') == 'app'


def configured():
    return bool(os.getenv('WEB_PUSH_PUBLIC_KEY') and os.getenv('WEB_PUSH_PRIVATE_KEY') and os.getenv('WEB_PUSH_SUBJECT'))


def validate_subscription(value):
    try:
        endpoint, keys = value['endpoint'], value['keys']
        url = urlsplit(endpoint)
        if (not isinstance(endpoint, str) or not 20 <= len(endpoint) <= 2048
                or url.scheme != 'https' or url.username or url.password or url.port not in (None, 443)
                or url.fragment or url.hostname not in {'fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com'}):
            raise ValueError
        def decode(key):
            raw = keys[key]
            if not isinstance(raw, str) or len(raw) > 150:
                raise ValueError
            return base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
        public, auth = decode('p256dh'), decode('auth')
        if len(public) != 65 or public[0] != 4 or len(auth) != 16:
            raise ValueError
        from cryptography.hazmat.primitives.asymmetric import ec
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
        return {'endpoint': endpoint, 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']}}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AccessError('Некорректная подписка уведомлений. Включите их заново в профиле.', 400) from None


async def queue_notice(session, user_id, *, kind, event_key, url='/app/#home', message='Новое рабочее событие в Photo Boss.', payload=None):
    existing = await session.scalar(select(Notification.id).where(Notification.user_id == user_id, Notification.event_key == event_key))
    if existing:
        return existing
    row = Notification(user_id=user_id, text=message, kind=kind, event_key=event_key,
                       payload=json.dumps({'url': url, **(payload or {})}), sent=False)
    session.add(row)
    await session.flush()
    return row.id


async def queue_for_telegram_id(engine, tg_id, *, kind='chat', message='Новое сообщение в Photo Boss.', url='/app/#home'):
    async with AsyncSession(engine) as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id, User.active.is_(True)))
        if not user:
            return False
        await queue_notice(session, user.id, kind=kind, event_key=f'{kind}:{uuid4().hex}', url=url, message=message)
        await session.commit()
    return True


async def scoped_supervisors(session, hotel_id):
    from .models import HotelEmployee
    return set(await session.scalars(select(UserRole.user_id).where(
        (UserRole.role == 'OWNER') | ((UserRole.role == 'ADMIN') & UserRole.user_id.in_(
            select(HotelEmployee.user_id).where(HotelEmployee.hotel_id == hotel_id))))))


async def queue_sale_ready(session, booking):
    recipients = await scoped_supervisors(session, booking.hotel_id) | {booking.manager_id, booking.photographer_id}
    description = await booking_description(session, booking)
    for uid in sorted(value for value in recipients if value is not None):
        user = await session.get(User, uid)
        if user and user.active:
            await queue_notice(session, uid, kind='sale_ready', event_key=f'sale-ready:{booking.id}',
                url=f'/app/#shootings?booking={booking.id}', message=description,
                payload={'bookingId': booking.id, 'description': description})


async def queue_sale_created(session, sale, booking):
    """Same sale transaction: related staff and scoped supervisors, one event."""
    supervisors = await scoped_supervisors(session, booking.hotel_id)
    recipients = supervisors | {booking.manager_id, sale.credited_user_id}
    for uid in sorted(value for value in recipients if value is not None):
        user = await session.get(User, uid)
        if user and user.active:
            await queue_notice(session, uid, kind='sale_created', event_key=f'sale-created:{sale.id}',
                url='/app/#finance', message=await booking_description(session, booking),
                payload={'bookingId': booking.id})


def current_utc():
    moment = utc_now()
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def reminder_deadline(notice):
    if notice.kind != 'booking_reminder':
        return None
    try:
        moment = datetime.fromisoformat(json.loads(notice.payload or '{}')['scheduledAt'])
        return moment.astimezone(UTC) if moment.tzinfo else None
    except (KeyError, TypeError, ValueError):
        return None


async def attendance_recipient(session, user, roles, target_user_id, day):
    from zoneinfo import ZoneInfo

    from .config import config
    from .miniapp_security import utc_bounds
    from .models import HotelEmployee, Shift

    if 'OWNER' in roles or target_user_id == user.id:
        return True
    if 'ADMIN' not in roles:
        return False
    lo, hi = utc_bounds(day, day, ZoneInfo(config.training_timezone))
    hotels = set(await session.scalars(select(Shift.hotel_id).where(
        Shift.user_id == target_user_id, Shift.status != 'CANCELLED',
        Shift.start_at < hi, Shift.end_at > lo)))
    allowed = set(await session.scalars(select(HotelEmployee.hotel_id).where(
        HotelEmployee.user_id == user.id)))
    # Same rule as attendance review: every planned hotel must be in scope.
    return bool(hotels) and hotels <= allowed


async def eligible(session, notice, sub, *, include_read=False, for_delivery=False):
    if not sub.active or sub.user_id != notice.user_id or notice.resolved_at or (notice.acknowledged_at and not include_read):
        return False
    user = await session.get(User, notice.user_id)
    roles = set(await session.scalars(select(UserRole.role).where(UserRole.user_id == notice.user_id)))
    if not user or not user.active or not roles & ROLES:
        return False
    if notice.kind in {'work_event', 'service_status'}:
        return 'OWNER' in roles
    if notice.kind in {'manager_idle', 'manager_advice'}:
        from .booking_control import preferences
        from .models import HotelEmployee, Shift, ShiftCheckIn, ShiftCheckOut
        if not (await preferences(session))['enabled']:
            return False
        try:
            details = json.loads(notice.payload or '{}')
            hotels = details['hotelIds']
            if (not isinstance(hotels, list) or not hotels
                    or any(type(hid) is not int or hid <= 0 for hid in hotels)):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        if notice.kind == 'manager_advice':
            if 'OWNER' in roles:
                return True
            allowed = set(await session.scalars(select(HotelEmployee.hotel_id).where(
                HotelEmployee.user_id == user.id)))
            # A previous message can contain several hotels and staff names.
            # Losing any scope hides the entire old message rather than leaking it.
            return bool('ADMIN' in roles and set(hotels) <= allowed)
        if ('MANAGER' not in roles or details.get('managerId') != user.id
                or type(details.get('shiftId')) is not int or len(hotels) != 1):
            return False
        shift = await session.get(Shift, details['shiftId'])
        now = current_utc().replace(tzinfo=None)
        if (not shift or shift.user_id != user.id or shift.hotel_id != hotels[0]
                or shift.status == 'CANCELLED' or not shift.start_at <= now < shift.end_at):
            return False
        from zoneinfo import ZoneInfo

        from .config import config
        day = current_utc().astimezone(ZoneInfo(config.training_timezone)).date()
        started = await session.scalar(select(ShiftCheckIn.id).where(
            ShiftCheckIn.user_id == user.id, ShiftCheckIn.shift_date == day,
            ShiftCheckIn.status == 'STARTED'))
        ended = await session.scalar(select(ShiftCheckOut.id).where(
            ShiftCheckOut.user_id == user.id, ShiftCheckOut.shift_date == day,
            ShiftCheckOut.status == 'FINISHED'))
        return bool(started and not ended)
    if notice.kind in {'booking_assignment', 'booking_reminder'}:
        from .models import Booking
        from .services.assignment_notifications import ACTIVE
        bid = notice_booking_id(notice)
        booking = await session.get(Booking, bid) if type(bid) is int else None
        trashed = await session.get(Setting, f'booking:trash:{bid}')
        if not (booking and booking.photographer_id == user.id and 'PHOTOGRAPHER' in roles and not trashed):
            return False
        if notice.kind == 'booking_reminder':
            from .services.booking_reminders import PRE_SHOOT, scheduled_at
            moment = scheduled_at(booking)
            return (booking.status in PRE_SHOOT
                    and json.loads(notice.payload or '{}').get('scheduledAt') == moment.isoformat()
                    and (not for_delivery or moment - timedelta(minutes=20) <= current_utc() < moment))
        return booking.status in ACTIVE
    if notice.kind == 'sale_ready' or (notice.kind == 'reminder' and notice_booking_id(notice)):
        from .models import Booking, HotelEmployee
        bid = notice_booking_id(notice)
        booking = await session.get(Booking, bid) if bid else None
        if not booking or await session.get(Setting, f'booking:trash:{bid}'):
            return False
        if 'OWNER' in roles or user.id in {booking.manager_id, booking.photographer_id}:
            return True
        return bool('ADMIN' in roles and await session.scalar(select(HotelEmployee.id).where(
            HotelEmployee.user_id == user.id, HotelEmployee.hotel_id == booking.hotel_id).limit(1)))
    if notice.kind in {'lunch_start', 'lunch_end'}:
        from .models import HotelEmployee, WorkBreak
        try:
            break_id = json.loads(notice.payload or '{}').get('workBreakId')
        except (TypeError, ValueError):
            return False
        row = await session.get(WorkBreak, break_id) if type(break_id) is int else None
        if not row:
            return False
        if 'OWNER' in roles or row.user_id == user.id:
            return True
        return bool('ADMIN' in roles and await session.scalar(select(HotelEmployee.id).where(
            HotelEmployee.user_id == user.id, HotelEmployee.hotel_id == row.hotel_id).limit(1)))
    if notice.kind == 'attendance':
        from .models import ShiftCheckIn, ShiftCheckOut
        try:
            data = json.loads(notice.payload or '{}')
            purpose, attendance_id = data.get('purpose'), data.get('attendanceId')
            model = {'start': ShiftCheckIn, 'end': ShiftCheckOut}.get(purpose)
            if model is None or type(attendance_id) is not int:
                return False
            row = await session.get(model, attendance_id)
            if (not row or data.get('userId') != row.user_id
                    or data.get('date') != row.shift_date.isoformat()):
                return False
        except (TypeError, ValueError):
            return False
        return await attendance_recipient(session, user, roles, row.user_id, row.shift_date)
    if notice.kind == 'attendance_fine':
        from zoneinfo import ZoneInfo

        from .attendance_fines import LABELS, fine_key
        from .config import config
        try:
            data = json.loads(notice.payload or '{}')
            uid, day = data['userId'], date.fromisoformat(data['date'])
            kind = data['fineKind']
            key = fine_key(uid, day, kind)
            if type(uid) is not int or kind not in LABELS or data.get('fineKey') != key:
                return False
            record = await session.get(Setting, key)
            charge = json.loads(record.value) if record else None
            if (not charge or charge.get('userId') != uid or charge.get('date') != str(day)
                    or charge.get('kind') != kind or (for_delivery and
                        (data.get('noticeRevision') != charge.get('noticeRevision')
                         or data.get('observedAt') != charge.get('observedAt')))):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        if (for_delivery and not data.get('changed')
                and day < current_utc().astimezone(ZoneInfo(config.training_timezone)).date()):
            return False  # Retained historical plans must not produce fresh phone alerts.
        return await attendance_recipient(session, user, roles, uid, day)
    if notice.kind == 'sale_discount':
        from .models import Booking, SaleDiscountApproval
        from .services.booking_access import can_access_booking
        try:
            data = json.loads(notice.payload or '{}')
            discount_id, bid = data.get('discountId'), data.get('bookingId')
        except (TypeError, ValueError):
            return False
        if type(discount_id) is not int or type(bid) is not int:
            return False
        approval = await session.get(SaleDiscountApproval, discount_id)
        booking = await session.get(Booking, bid)
        if (not approval or approval.booking_id != bid or not booking
                or await session.get(Setting, f'booking:trash:{bid}')
                or not await can_access_booking(session, user.id, roles, booking)):
            return False
        return bool(roles & {'OWNER', 'ADMIN'} or approval.requested_by_id == user.id)
    if notice.kind == 'sale_created':
        from .models import Booking, HotelEmployee, Sale
        try:
            sale_id = int(notice.event_key.rsplit(':', 1)[1])
        except (ValueError, AttributeError):
            return False
        sale = await session.get(Sale, sale_id)
        booking = await session.get(Booking, sale.booking_id) if sale else None
        if not booking:
            return False
        if 'OWNER' in roles or user.id in {booking.manager_id, sale.credited_user_id}:
            return True
        return bool('ADMIN' in roles and await session.scalar(select(HotelEmployee.id).where(
            HotelEmployee.user_id == user.id, HotelEmployee.hotel_id == booking.hotel_id).limit(1)))
    if notice.kind in {'daily_summary', 'control'}:
        if 'OWNER' not in roles:
            return False
        if notice.kind == 'daily_summary':
            pref = await session.get(Setting, f'notify:owner:{user.id}')
            return bool(pref and json.loads(pref.value).get('daily'))
        pref = await session.get(Setting, f'notify:owner:{user.id}')
        return bool(pref and json.loads(pref.value).get('critical') and notice.priority == 'critical')
    return True


def payload_for(notice):
    urls = {'booking_assignment': '/app/#shootings', 'control': '/app/#insights', 'daily_summary': '/app/#insights',
            'sale_ready': '/app/#workflow', 'sale_created': '/app/#finance', 'academy': '/app/#academy',
            'attendance': '/app/#workday', 'attendance_fine': '/app/#finance', 'sale_discount': '/app/#workflow',
            'manager_idle': '/app/#home', 'manager_advice': '/app/#home'}
    titles = {'service_status': 'Состояние подключений', 'work_event': 'Рабочее событие', 'booking_assignment': 'Назначена съёмка', 'booking_reminder': 'Скоро съёмка', 'chat': 'Новое сообщение', 'call': 'Входящий звонок',
              'sale_ready': 'Съёмка готова к продаже', 'sale_created': 'Оформлена продажа', 'academy': 'Готов разбор практики', 'push_test': 'Проверка уведомлений',
              'lunch_start': 'Сотрудник ушёл на обед', 'lunch_end': 'Сотрудник вернулся с обеда',
              'attendance': 'Отметка смены', 'attendance_fine': 'Контроль времени смены',
              'sale_discount': 'Согласование скидки 50%', 'manager_idle': 'Пора пригласить гостей на съёмку',
              'manager_advice': 'Проверьте записи и команду отелей'}
    try:
        details = json.loads(notice.payload or '{}')
    except (TypeError, ValueError):
        details = {}
    if notice.kind == 'attendance':
        titles['attendance'] = ('Отметка начала смены' if details.get('purpose') == 'start'
                                else 'Отметка завершения смены')
    if notice.kind == 'attendance_fine':
        titles['attendance_fine'] = ('Начисление за смену изменено' if details.get('changed') else
            'Нет отметки прихода' if details.get('fineKind') == 'late' and not details.get('observedAt') else
            'Опоздание: проверьте отметку' if details.get('fineKind') == 'late' else 'Контроль времени смены')
    if notice.kind == 'reminder' and (notice.event_key or '').startswith(('rescheduled:', 'cancelled:')):
        titles['reminder'] = 'Съёмка перенесена' if notice.event_key.startswith('rescheduled:') else 'Съёмка отменена'
    # No guest names, phone numbers, rooms, balances, or chat text on the lock screen.
    url = urls.get(notice.kind, '/app/#home')
    if notice.kind in {'booking_assignment', 'booking_reminder', 'sale_ready'}:
        url = '/app/#shootings'
        bid = notice_booking_id(notice)
        if type(bid) is int:
            url += f'?booking={bid}'
    if notice.kind == 'sale_discount':
        bid = notice_booking_id(notice)
        if type(bid) is int:
            url += f'?booking={bid}'
    if notice.kind == 'attendance' and details.get('canReview'):
        url = '/app/#attendance'
    if notice.kind == 'attendance_fine' and details.get('canReview') and isinstance(details.get('fineKey'), str):
        url = '/app/#more?attendanceFine=' + quote(details['fineKey'], safe='')
    payload = {'title': 'Photo Boss · ' + titles.get(notice.kind, 'Рабочее уведомление'),
            'body': 'Откройте Photo Boss, чтобы посмотреть подробности.', 'url': url, 'tag': f'pb-notice-{notice.id}'}
    if notice.kind == 'booking_reminder' and isinstance(details.get('pushSummary'), str):
        payload['body'] = details['pushSummary'][:180] + '. Откройте съёмку в Photo Boss.'
    deadline = reminder_deadline(notice)
    if deadline:
        payload['expiresAt'] = deadline.isoformat()
    return payload


async def send_push(subscription, payload):
    from pywebpush import WebPushException, webpush
    ttl = 300 if payload['title'].endswith('звонок') else 3600
    if payload.get('expiresAt'):
        remaining = (datetime.fromisoformat(payload['expiresAt']) - current_utc()).total_seconds()
        if remaining <= 0:
            return None  # The reminder expired before the provider request began.
        ttl = min(ttl, int(remaining))
    try:
        response = await asyncio.to_thread(webpush, subscription_info=subscription,
            data=json.dumps(payload, ensure_ascii=False), vapid_private_key=os.environ['WEB_PUSH_PRIVATE_KEY'],
            vapid_claims={'sub': os.environ['WEB_PUSH_SUBJECT']}, ttl=ttl,
            timeout=10, headers={'Urgency': 'high'})
        return response.status_code
    except WebPushException as exc:
        return exc.status_code or 503
    except (OSError, ValueError):
        return 503


async def deliver_push(engine, *, limit=20):
    if not configured() or not app_channel():
        return 0
    accepted = 0
    async with AsyncSession(engine) as session:
        # A fresh subscription never receives historical alerts.
        pairs = (await session.execute(select(Notification, PushSubscription).join(PushSubscription,
            PushSubscription.user_id == Notification.user_id).where(PushSubscription.active.is_(True),
            Notification.kind.in_(KINDS), Notification.created_at >= PushSubscription.created_at,
            Notification.created_at >= utc_now() - timedelta(days=1), Notification.resolved_at.is_(None),
            ~select(PushDelivery.id).where(PushDelivery.notification_id == Notification.id,
                PushDelivery.subscription_id == PushSubscription.id).exists())
            .order_by(Notification.id).limit(100))).all()
        for notice, sub in pairs:
            status = 'PENDING' if await eligible(session, notice, sub, for_delivery=True) else 'SKIPPED'
            try:
                async with session.begin_nested():
                    session.add(PushDelivery(notification_id=notice.id, subscription_id=sub.id, status=status))
                    await session.flush()
            except IntegrityError:
                # Two instances may overlap during a zero-downtime release.
                pass
        await session.commit()
        ids = list(await session.scalars(select(PushDelivery.id).where(PushDelivery.status == 'PENDING',
            PushDelivery.next_attempt_at <= utc_now()).order_by(PushDelivery.id).limit(limit)))
    for did in ids:
        async with AsyncSession(engine) as session:
            delivery = await session.scalar(select(PushDelivery).where(PushDelivery.id == did)
                .with_for_update(skip_locked=True))
            if not delivery or delivery.status != 'PENDING':
                continue
            sub = await session.get(PushSubscription, delivery.subscription_id, with_for_update=True)
            notice = await session.get(Notification, delivery.notification_id)
            if (not notice or not sub or not await eligible(session, notice, sub, for_delivery=True)
                    or notice.created_at < sub.created_at
                    or notice.created_at < utc_now() - timedelta(seconds=60 if notice.kind == 'call' else 86400)):
                delivery.status = 'SKIPPED'
            else:
                code = await send_push(json.loads(sub.subscription), payload_for(notice))
                if code is None:
                    delivery.status = 'SKIPPED'
                    await session.commit()
                    continue
                delivery.attempts += 1
                delivery.last_error = None if 200 <= code < 300 else f'provider_http_{code}'
                if 200 <= code < 300:
                    delivery.status, delivery.accepted_at = 'ACCEPTED', utc_now()
                    accepted += 1
                elif code in {404, 410}:
                    sub.active, delivery.status = False, 'EXPIRED'
                else:
                    delivery.status = 'FAILED' if delivery.attempts >= 8 else 'PENDING'
                    delivery.next_attempt_at = utc_now() + timedelta(seconds=min(1800, 15 * 2 ** min(delivery.attempts, 7)))
            await session.commit()
    return accepted


async def push_loop(engine):
    while True:
        try:
            from .operations_guard import operation_lease
            async with operation_lease(engine):
                await deliver_push(engine)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable queue retries; no endpoint or credential logging
            log.warning('App notification delivery deferred (%s)', type(exc).__name__)
        await asyncio.sleep(5)


async def log_push_state(engine):
    async with AsyncSession(engine) as session:
        devices = await session.scalar(select(func.count(PushSubscription.id)).where(PushSubscription.active.is_(True)))
        pending = await session.scalar(select(func.count(PushDelivery.id)).where(PushDelivery.status == 'PENDING'))
    log.info('App push status: channel=%s configured=%s active_devices=%s pending_deliveries=%s',
             'app' if app_channel() else 'telegram', configured(), devices, pending)


async def push_delivery_state(session, notice, *, active_devices):
    """Report provider state without claiming that a phone displayed the alert."""
    deadline = reminder_deadline(notice)
    if deadline and current_utc() >= deadline:
        return 'EXPIRED'
    if not app_channel() or not configured():
        return 'OFF'
    if not active_devices:
        return 'NO_DEVICE'
    rows = list(await session.scalars(select(PushDelivery).where(
        PushDelivery.notification_id == notice.id)))
    states = {row.status for row in rows}
    if not rows:
        subscribed_before = await session.scalar(select(PushSubscription.id).where(
            PushSubscription.user_id == notice.user_id, PushSubscription.active.is_(True),
            PushSubscription.created_at <= notice.created_at).limit(1))
        # New installations do not receive historical alerts; do not promise a retry.
        return 'PENDING' if subscribed_before else 'HISTORY_ONLY'
    if 'PENDING' in states:
        return 'PENDING'
    if 'ACCEPTED' in states and (states & {'FAILED', 'EXPIRED'}):
        return 'PARTIAL'
    if 'ACCEPTED' in states:
        return 'ACCEPTED'
    if states & {'FAILED', 'EXPIRED'}:
        return 'FAILED'
    if states == {'SKIPPED'}:
        return 'SKIPPED'
    return 'PENDING'


class AppPush:
    def __init__(self, api):
        self.api = api

    async def actor(self, session, request):
        user = await session.get(User, request['miniapp_actor']['id'], with_for_update={'key_share': True})
        roles = set(await session.scalars(select(UserRole.role).where(UserRole.user_id == user.id))) if user else set()
        if not user or not user.active or not roles & ROLES:
            raise AccessError('Рабочий доступ отключён.', 403)
        return user

    async def status(self, request):
        from .config import config
        from .models import OwnerTelegramDelivery
        from .owner_telegram import START_KEY
        async with AsyncSession(self.api.engine) as session:
            actor = await self.actor(session, request)
            owner_copy = actor.tg_id in config.admin_ids and bool(await session.scalar(
                select(UserRole.id).where(UserRole.user_id == actor.id, UserRole.role == 'OWNER')))
            mirror_start = await session.get(Setting, START_KEY) if owner_copy else None
            rows = list(await session.scalars(select(Notification).where(Notification.user_id == actor.id,
                Notification.kind.in_(KINDS), Notification.resolved_at.is_(None)).order_by(Notification.id.desc()).limit(30)))
            active_devices = await session.scalar(select(func.count(PushSubscription.id)).where(
                PushSubscription.user_id == actor.id, PushSubscription.active.is_(True)))
            items = []
            for notice in rows:
                if not await eligible(session, notice, SimpleNamespace(active=True, user_id=actor.id), include_read=True):
                    continue
                payload = json.loads(notice.payload or '{}')
                description = payload.get('description') or notice.text
                bid = notice_booking_id(notice)
                if bid and notice.kind in {'booking_assignment', 'booking_reminder', 'sale_ready', 'sale_created', 'reminder'}:
                    from .models import Booking
                    booking = await session.get(Booking, bid)
                    if booking:
                        description = await booking_description(session, booking)
                copy = await session.get(OwnerTelegramDelivery, notice.id) if owner_copy else None
                telegram_state = (copy.status if copy else 'PENDING' if mirror_start and
                    notice.created_at >= datetime.fromisoformat(mirror_start.value) else 'HISTORY_ONLY') if owner_copy else None
                items.append({'id': notice.id, 'title': payload_for(notice)['title'],
                    'kind': notice.kind,
                    'description': description,
                    'url': payload_for(notice)['url'], 'read': bool(notice.acknowledged_at),
                    'createdAt': notice.created_at.isoformat()+'Z',
                    'pushState': await push_delivery_state(session, notice, active_devices=active_devices),
                    'ownerTelegramState': telegram_state})
            return web.json_response({'configured': configured(), 'channel': 'app' if app_channel() else 'telegram',
                'ownerTelegram': owner_copy and app_channel(),
                'activeDevices': active_devices,
                'publicKey': os.getenv('WEB_PUSH_PUBLIC_KEY') if configured() else None,
                'items': items}, headers={'Cache-Control':'no-store'})

    async def subscribe(self, request):
        if not configured():
            raise AccessError('Push-уведомления ещё настраиваются на сервере.', 503)
        body = await self.api.body(request)
        value = validate_subscription(body.get('subscription'))
        digest = hashlib.sha256(value['endpoint'].encode()).hexdigest()
        async with AsyncSession(self.api.engine) as session:
            actor = await self.actor(session, request)
            row = await session.scalar(select(PushSubscription).where(PushSubscription.endpoint_hash == digest).with_for_update())
            if row:
                # Rebinding an installation invalidates pending deliveries of the old account.
                if row.user_id != actor.id or not row.active:
                    row.created_at = utc_now()
                row.user_id, row.subscription, row.active, row.updated_at = actor.id, json.dumps(value), True, utc_now()
            else:
                session.add(PushSubscription(user_id=actor.id, endpoint_hash=digest, subscription=json.dumps(value)))
            await session.commit()
        return web.json_response({'ok': True})

    async def read(self, request):
        from .people import positive_id
        body = await self.api.body(request)
        if set(body) != {'id'}:
            raise AccessError('Укажите уведомление.', 400)
        nid = positive_id(body['id'])
        async with AsyncSession(self.api.engine) as session:
            actor = await self.actor(session, request)
            notice = await session.get(Notification, nid, with_for_update=True)
            if not notice or notice.user_id != actor.id or notice.kind not in KINDS:
                raise AccessError('Уведомление не найдено.', 404)
            notice.acknowledged_at = notice.acknowledged_at or utc_now()
            await session.commit()
        return web.json_response({'ok': True})

    async def unsubscribe(self, request):
        body = await self.api.body(request)
        endpoint = body.get('endpoint')
        if not isinstance(endpoint, str):
            raise AccessError('Подписка не указана.', 400)
        async with AsyncSession(self.api.engine) as session:
            actor = await self.actor(session, request)
            row = await session.scalar(select(PushSubscription).where(PushSubscription.user_id == actor.id,
                PushSubscription.endpoint_hash == hashlib.sha256(endpoint.encode()).hexdigest()).with_for_update())
            if row:
                row.active = False
            await session.commit()
        return web.json_response({'ok': True})

    async def test(self, request):
        async with AsyncSession(self.api.engine) as session:
            actor = await self.actor(session, request)
            latest = await session.scalar(select(Notification).where(Notification.user_id == actor.id,
                Notification.kind == 'push_test', Notification.created_at > utc_now()-timedelta(seconds=30)))
            if latest:
                raise AccessError('Проверка уже отправлена. Подождите 30 секунд.', 429)
            await queue_notice(session, actor.id, kind='push_test', event_key='test:'+uuid4().hex)
            await session.commit()
        return web.json_response({'queued': True})


def install_push(app, api):
    service = AppPush(api)
    app.router.add_get('/api/miniapp/push', service.status)
    app.router.add_post('/api/miniapp/push/subscribe', service.subscribe)
    app.router.add_post('/api/miniapp/push/unsubscribe', service.unsubscribe)
    app.router.add_post('/api/miniapp/push/test', service.test)
    app.router.add_post('/api/miniapp/push/read', service.read)
