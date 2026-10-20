"""Durable photographer assignment notices, shared by web and bot workflows."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..miniapp_security import AccessError
from ..models import Booking, Hotel, Notification, User, UserRole, utc_now
from ..operations_guard import operation_lease

logger = logging.getLogger(__name__)
KIND = 'booking_assignment'
ACTIVE = {'NEW', 'PENDING_CONFIRMATION', 'CONFIRMED', 'ASSIGNED', 'RESCHEDULED'}


async def queue_assignment(conn, booking_id, photographer_id):
    """Call under the booking lock in the same transaction as assignment."""
    prefix = f'booking-assigned:{booking_id}:'
    found = (await conn.execute(text('''SELECT id,user_id FROM notifications
        WHERE kind=:kind AND event_key LIKE :prefix AND resolved_at IS NULL
        ORDER BY id DESC'''), {'kind': KIND, 'prefix': prefix+'%'})).mappings().all()
    if found and found[0]['user_id'] == photographer_id:
        return found[0]['id']
    now = utc_now()
    await conn.execute(text('''UPDATE notifications SET resolved_at=:now
        WHERE kind=:kind AND event_key LIKE :prefix AND resolved_at IS NULL'''),
        {'kind': KIND, 'prefix': prefix+'%', 'now': now})
    rows = (await conn.execute(text('''INSERT INTO notifications
        (user_id,text,sent,created_at,event_key,priority,kind,payload)
        VALUES (:uid,:message,FALSE,:now,:key,'info',:kind,:payload) RETURNING id'''),
        {'uid': photographer_id, 'message': f'Вам назначена съёмка №{booking_id}.',
         'now': now, 'key': prefix+uuid.uuid4().hex, 'kind': KIND,
         'payload': json.dumps({'bookingId': booking_id})})).mappings().all()
    return rows[0]['id']


async def assignment_items(conn, user_id, today):
    rows = (await conn.execute(text('''SELECT n.id,b.id AS booking_id,b.shoot_date,
        b.shoot_time,b.room,h.name AS hotel,c.name AS client FROM notifications n
        JOIN bookings b ON n.event_key LIKE 'booking-assigned:' || CAST(b.id AS VARCHAR) || ':%'
        JOIN hotels h ON h.id=b.hotel_id
        JOIN clients c ON c.id=b.client_id
        WHERE n.user_id=:uid AND b.photographer_id=:uid AND n.kind=:kind
        AND n.resolved_at IS NULL AND b.shoot_date>=:today
        AND b.status IN ('NEW','PENDING_CONFIRMATION','CONFIRMED','ASSIGNED','RESCHEDULED')
        AND NOT EXISTS (SELECT 1 FROM settings st WHERE st.key='booking:trash:' || CAST(b.id AS VARCHAR))
        ORDER BY b.shoot_date,b.shoot_time,n.id DESC LIMIT 20'''),
        {'uid': user_id, 'kind': KIND, 'today': today})).mappings().all()
    return [{'id': r['id'], 'bookingId': r['booking_id'], 'hotel': r['hotel'], 'client': r['client'],
             'date': str(r['shoot_date']), 'time': str(r['shoot_time'])[:5], 'room': r['room']} for r in rows]


async def _pending_assignment_ids(session):
    """Scan a fixed queue snapshot without holding discovery transactions open."""
    pending = (Notification.kind == KIND, Notification.sent.is_(False),
               Notification.resolved_at.is_(None))
    high_water = await session.scalar(select(func.max(Notification.id)).where(*pending)) or 0
    await session.rollback()
    cursor = 0
    while cursor < high_water:
        ids = list(await session.scalars(select(Notification.id).where(*pending,
            Notification.id > cursor, Notification.id <= high_water)
            .order_by(Notification.id).limit(100)))
        await session.rollback()
        if not ids:
            return
        for nid in ids:
            cursor = nid
            yield nid


async def deliver_assignments(engine, bot, *, booking_id=None, limit=10):
    """Booking -> notice -> user lock order matches assignment transactions."""
    from ..push_notifications import app_channel
    if app_channel():
        return 0  # The durable notice is delivered by the app push worker.
    sent, attempted = 0, 0
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async for nid in _pending_assignment_ids(session):
            if attempted >= limit:
                break
            candidate = await session.get(Notification, nid)
            if candidate is None:
                await session.rollback()
                continue
            try:
                payload = json.loads(candidate.payload or '{}')
                bid = payload.get('bookingId')
                due = payload.get('nextAttemptAt')
                if due and datetime.fromisoformat(due) > utc_now():
                    await session.rollback()
                    continue
            except (TypeError, ValueError):
                bid = None
            if type(bid) is not int or (booking_id is not None and bid != booking_id):
                await session.rollback()
                continue
            booking = await session.scalar(select(Booking).where(Booking.id == bid)
                .with_for_update(skip_locked=True))
            if booking is None:
                await session.rollback()
                continue
            notice = await session.scalar(select(Notification).where(Notification.id == nid)
                .with_for_update(skip_locked=True).execution_options(populate_existing=True))
            if notice is None or notice.sent or notice.resolved_at is not None:
                await session.rollback()
                continue
            user = await session.scalar(select(User).where(User.id == notice.user_id).with_for_update())
            role = await session.scalar(select(UserRole.id).where(
                UserRole.user_id == notice.user_id, UserRole.role == 'PHOTOGRAPHER'))
            trashed = await session.scalar(text('SELECT 1 FROM settings WHERE key=:key'),
                {'key': f'booking:trash:{bid}'})
            if (booking.photographer_id != notice.user_id or booking.status not in ACTIVE
                    or user is None or not user.active or not role or trashed):
                notice.resolved_at = utc_now()
                await session.commit()
                continue
            hotel = await session.get(Hotel, booking.hotel_id)
            from ..launch_policy import app_url
            url = app_url().split('#', 1)[0] + f'#shootings?booking={bid}'
            notice.text = (f'📸 Вам назначена съёмка №{bid}\n'
                f'🏨 {hotel.name if hotel else "Отель"}\n'
                f'📅 {booking.shoot_date:%d.%m.%Y} · {booking.shoot_time:%H:%M}\n'
                f'🚪 Комната: {booking.room}\n\nОткройте съёмку в Photo Boss.')
            try:
                attempted += 1
                await bot.send_message(user.tg_id, notice.text, parse_mode=None,
                    disable_notification=False, request_timeout=5,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text='📱 Открыть съёмку', web_app=WebAppInfo(url=url))]]))
            except (TelegramAPIError, TimeoutError, OSError):
                failures = min(int(payload.get('failures', 0))+1, 8)
                payload.update(failures=failures,
                    nextAttemptAt=(utc_now()+timedelta(seconds=min(1800, 30*2**(failures-1)))).isoformat())
                notice.payload = json.dumps(payload)
                await session.commit()
                continue
            notice.sent = True
            await session.commit()
            sent += 1
    return sent


async def assignment_loop(engine, bot, *, interval=5):
    while True:
        try:
            async with operation_lease(engine):
                await deliver_assignments(engine, bot, limit=3)
        except asyncio.CancelledError:
            raise
        except AccessError as exc:
            if exc.status != 503:
                logger.exception('Assignment notification delivery rejected')
        except Exception:
            logger.exception('Assignment notification delivery unavailable; notices remain queued')
        await asyncio.sleep(interval)
