"""Guest and hotel context for authenticated in-app booking notifications."""
import json
import re

from ..models import Client, Hotel


def notice_booking_id(notice):
    try:
        bid = json.loads(notice.payload or '{}').get('bookingId')
        if type(bid) is int and bid > 0:
            return bid
    except (TypeError, ValueError, AttributeError):
        pass
    key = notice.event_key or ''
    if key.startswith(('sale-ready:', 'booking-assigned:', 'shoot-reminder:', 'rescheduled:', 'cancelled:')):
        try:
            return int(key.split(':')[1])
        except (ValueError, IndexError):
            return None
    if notice.kind == 'sale_ready':
        match = re.search(r'Съ[её]мка\s*[№#]\s*(\d+)', notice.text or '', re.IGNORECASE)
        return int(match[1]) if match else None
    return None


async def booking_description(session, booking):
    hotel = await session.get(Hotel, booking.hotel_id)
    client = await session.get(Client, booking.client_id)
    lines = [f'Отель: {hotel.name if hotel else "не указан"}',
             f'Гость: {client.name if client and client.name else "имя не указано"}',
             f'Дата: {booking.shoot_date:%d.%m.%Y} в {booking.shoot_time:%H:%M}',
             f'Комната: {booking.room}', f'Гостей: {booking.guest_count}']
    if hotel and hotel.address:
        lines.append(f'Место: {hotel.address}')
    return '\n'.join(lines)


async def queue_photographer_change(session, booking, change):
    """Queue inside the booking transaction, before its caller commits."""
    from uuid import uuid4

    from ..models import User
    from ..push_notifications import queue_notice

    if change not in {'rescheduled', 'cancelled'}:
        raise ValueError('Unsupported booking change')
    photographer = await session.get(User, booking.photographer_id) if booking.photographer_id else None
    if not photographer or not photographer.active:
        return False
    description = await booking_description(session, booking)
    if change == 'rescheduled':
        message = (f'Съёмка №{booking.id} перенесена на '
                   f'{booking.shoot_date:%d.%m.%Y} в {booking.shoot_time:%H:%M}.')
    else:
        message = f'Съёмка №{booking.id} отменена.'
        if booking.cancellation_reason:
            description += f'\nПричина: {booking.cancellation_reason}'
    # A later move back to a previous date is a new event too. The booking lock
    # and revision check prevent command retries from creating another notice.
    await queue_notice(session, photographer.id, kind='reminder',
        event_key=f'{change}:{booking.id}:{uuid4().hex}',
        url=f'/app/#shootings?booking={booking.id}', message=message,
        payload={'bookingId': booking.id, 'description': description})
    return True
