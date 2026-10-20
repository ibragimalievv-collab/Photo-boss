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
