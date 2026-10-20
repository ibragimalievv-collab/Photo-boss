"""Durable twenty-minute reminders, keyed to the current booking schedule."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..models import Booking, Client, Hotel, Setting, User, UserRole
from ..operations_guard import operation_lease

log = logging.getLogger(__name__)
PRE_SHOOT = {'NEW', 'PENDING_CONFIRMATION', 'CONFIRMED', 'ASSIGNED',
             'RESCHEDULED', 'PICKED_UP', 'ACCEPTED'}


def scheduled_at(booking):
    return datetime.combine(booking.shoot_date, booking.shoot_time).replace(
        tzinfo=ZoneInfo(config.training_timezone)).astimezone(UTC)


async def queue_twenty_minute_reminders(session, now=None):
    from ..push_notifications import queue_notice

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    day = now.astimezone(ZoneInfo(config.training_timezone)).date()
    bookings = list(await session.scalars(select(Booking).where(
        Booking.shoot_date.between(day, day + timedelta(days=1)),
        Booking.photographer_id.is_not(None), Booking.status.in_(PRE_SHOOT))
        .order_by(Booking.id).with_for_update(skip_locked=True)))
    queued = 0
    for booking in bookings:
        moment = scheduled_at(booking)
        reminder_at = moment - timedelta(minutes=20)
        if not reminder_at <= now < moment:
            continue
        user = await session.get(User, booking.photographer_id)
        role = await session.scalar(select(UserRole.id).where(
            UserRole.user_id == booking.photographer_id, UserRole.role == 'PHOTOGRAPHER'))
        if (not user or not user.active or not role
                or await session.get(Setting, f'booking:trash:{booking.id}')):
            continue
        stamp = moment.isoformat()
        key = f'shoot-reminder:{booking.id}:{stamp}:{user.id}'
        from ..models import Notification
        if await session.scalar(select(Notification.id).where(
                Notification.user_id == user.id, Notification.event_key == key)):
            continue
        hotel = await session.get(Hotel, booking.hotel_id)
        client = await session.get(Client, booking.client_id)
        place = hotel.name if hotel else 'Отель не указан'
        if hotel and hotel.address:
            place += f' · {hotel.address}'
        details = '\n'.join((
            f'Съёмка №{booking.id} · {booking.shoot_date:%d.%m.%Y} в {booking.shoot_time:%H:%M}',
            f'Место: {place}',
            f'Номер: {booking.room}',
            f'Гостей: {booking.guest_count}',
            f'Клиент: {client.name}' if client and client.name else '',
        ))
        details = '\n'.join(line for line in details.splitlines() if line)
        await queue_notice(session, user.id, kind='booking_reminder', event_key=key,
            url=f'/app/#shootings?booking={booking.id}',
            message=details,
            payload={'bookingId': booking.id, 'scheduledAt': stamp, 'description': details})
        queued += 1
    return queued


async def booking_reminder_loop(engine, *, interval=15):
    while True:
        try:
            async with AsyncSession(engine) as session, operation_lease(engine):
                count = await queue_twenty_minute_reminders(session)
                await session.commit()
                if count:
                    log.info('Twenty-minute photographer reminders queued: %s', count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable retry; no guest data in logs
            log.warning('Booking reminder cycle deferred (%s)', type(exc).__name__)
        await asyncio.sleep(interval)
