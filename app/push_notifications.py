"""Account-bound Web Push. Provider acceptance is distinct from device delivery."""
import asyncio
import base64
import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
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

log = logging.getLogger(__name__)
ROLES = {'OWNER', 'ADMIN', 'PHOTOGRAPHER', 'MANAGER'}
KINDS = {'booking_assignment', 'booking_reminder', 'control', 'daily_summary', 'chat', 'call', 'sale_ready', 'sale_created', 'academy', 'reminder', 'push_test'}


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


async def queue_sale_created(session, sale, booking):
    """Same sale transaction: related staff and scoped supervisors, one event."""
    from .models import HotelEmployee
    supervisors = set(await session.scalars(select(UserRole.user_id).where(
        (UserRole.role == 'OWNER') | ((UserRole.role == 'ADMIN') & UserRole.user_id.in_(
            select(HotelEmployee.user_id).where(HotelEmployee.hotel_id == booking.hotel_id))))))
    recipients = supervisors | {booking.manager_id, sale.credited_user_id}
    for uid in sorted(value for value in recipients if value is not None):
        user = await session.get(User, uid)
        if user and user.active:
            await queue_notice(session, uid, kind='sale_created', event_key=f'sale-created:{sale.id}',
                url='/app/#finance', message=f'Оформлена продажа №{sale.id}. Подробности в кассе.')


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


async def eligible(session, notice, sub, *, include_read=False, for_delivery=False):
    if not sub.active or sub.user_id != notice.user_id or notice.resolved_at or (notice.acknowledged_at and not include_read):
        return False
    user = await session.get(User, notice.user_id)
    roles = set(await session.scalars(select(UserRole.role).where(UserRole.user_id == notice.user_id)))
    if not user or not user.active or not roles & ROLES:
        return False
    if notice.kind in {'booking_assignment', 'booking_reminder'}:
        from .models import Booking
        from .services.assignment_notifications import ACTIVE
        bid = json.loads(notice.payload or '{}').get('bookingId')
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
            'sale_ready': '/app/#workflow', 'sale_created': '/app/#finance', 'academy': '/app/#academy'}
    titles = {'booking_assignment': 'Назначена съёмка', 'booking_reminder': 'Скоро съёмка', 'chat': 'Новое сообщение', 'call': 'Входящий звонок',
              'sale_ready': 'Съёмка готова к продаже', 'sale_created': 'Оформлена продажа', 'academy': 'Готов разбор практики', 'push_test': 'Проверка уведомлений'}
    # No guest names, phone numbers, balances, or chat text on the lock screen.
    url = urls.get(notice.kind, '/app/#home')
    if notice.kind in {'booking_assignment', 'booking_reminder'}:
        url = '/app/#shootings'
        bid = json.loads(notice.payload or '{}').get('bookingId')
        if type(bid) is int:
            url += f'?booking={bid}'
    payload = {'title': 'Photo Boss · ' + titles.get(notice.kind, 'Рабочее уведомление'),
            'body': 'Откройте Photo Boss, чтобы посмотреть подробности.', 'url': url, 'tag': f'pb-notice-{notice.id}'}
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
    if 'PENDING' in states or not rows:
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
        user = await session.get(User, request['miniapp_actor']['id'], with_for_update=True)
        roles = set(await session.scalars(select(UserRole.role).where(UserRole.user_id == user.id))) if user else set()
        if not user or not user.active or not roles & ROLES:
            raise AccessError('Рабочий доступ отключён.', 403)
        return user

    async def status(self, request):
        async with AsyncSession(self.api.engine) as session:
            actor = await self.actor(session, request)
            rows = list(await session.scalars(select(Notification).where(Notification.user_id == actor.id,
                Notification.kind.in_(KINDS), Notification.resolved_at.is_(None)).order_by(Notification.id.desc()).limit(30)))
            active_devices = await session.scalar(select(func.count(PushSubscription.id)).where(
                PushSubscription.user_id == actor.id, PushSubscription.active.is_(True)))
            items = []
            for notice in rows:
                if not await eligible(session, notice, SimpleNamespace(active=True, user_id=actor.id), include_read=True):
                    continue
                payload = json.loads(notice.payload or '{}')
                items.append({'id': notice.id, 'title': payload_for(notice)['title'],
                    'description': payload.get('description') or notice.text,
                    'url': payload_for(notice)['url'], 'read': bool(notice.acknowledged_at),
                    'createdAt': notice.created_at.isoformat()+'Z',
                    'pushState': await push_delivery_state(session, notice, active_devices=active_devices)})
            return web.json_response({'configured': configured(), 'channel': 'app' if app_channel() else 'telegram',
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
