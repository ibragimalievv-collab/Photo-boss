"""A walk-in sale creates its booking and draft in the same queued transaction."""
import json
from datetime import date, time

from sqlalchemy import select

from ..models import (
    Booking,
    Client,
    Hotel,
    HotelEmployee,
    Package,
    Shooting,
    User,
    UserRole,
)
from .core import audit
from .pricing import booking_unit_quote
from .sale_workflow import start_sale_draft


async def options(session, actor_id, roles):
    self_only = 'PHOTOGRAPHER' in roles and not roles & {'OWNER', 'ADMIN', 'MANAGER'}
    hotel_ids = set(await session.scalars(select(Hotel.id).where(Hotel.active.is_(True))))
    memberships = list((await session.execute(select(HotelEmployee.user_id, HotelEmployee.hotel_id))).all())
    if 'OWNER' not in roles:
        hotel_ids &= {hotel for user, hotel in memberships if user == actor_id}
    if not roles & {'OWNER', 'ADMIN', 'MANAGER', 'PHOTOGRAPHER'}:
        hotel_ids.clear()
    users = list(await session.scalars(select(User).where(User.active.is_(True), User.id.in_(
        select(UserRole.user_id).where(UserRole.role == 'PHOTOGRAPHER'))).order_by(User.id)))
    photographers = [{'id': user.id, 'name': user.name,
        'hotelIds': sorted(hotel_ids & {hotel for uid, hotel in memberships if uid == user.id})}
        for user in users if not self_only or user.id == actor_id]
    return {'hotelIds': sorted(hotel_ids),
            'defaultHotelId': next(iter(hotel_ids)) if len(hotel_ids) == 1 else None,
            'defaultPhotographerId': actor_id if self_only else None,
            'photographerLocked': self_only,
            'photographers': [p for p in photographers if p['hotelIds']]}


async def create(session, actor, roles, guest, *, now):
    if not isinstance(guest, dict) or set(guest) - {
            'name', 'phone', 'room', 'hotelId', 'photographerId', 'date', 'time', 'packageId'}:
        raise ValueError('Проверьте данные новой продажи.')
    permitted = await options(session, actor.id, roles)
    hid = guest.get('hotelId')
    if type(hid) is not int or hid not in permitted['hotelIds']:
        raise ValueError('Нет доступа к продаже в этом отеле.')
    hotel = await session.get(Hotel, hid, with_for_update={'read': True})
    if not hotel or not hotel.active:
        raise ValueError('Отель больше не доступен. Обновите данные.')
    photographer_id = guest.get('photographerId')
    if permitted['photographerLocked'] and photographer_id is None:
        photographer_id = actor.id
    if (type(photographer_id) is not int or not any(
            p['id'] == photographer_id and hid in p['hotelIds'] for p in permitted['photographers'])):
        raise ValueError('Выберите действующего фотографа этого отеля.')
    try:
        name = guest['name'].strip()
        phone = guest.get('phone') or None
        room = (guest.get('room') or '').strip()
        day = date.fromisoformat(guest['date']) if guest.get('date') else now.date()
        at = time.fromisoformat(guest['time']) if guest.get('time') else now.time().replace(second=0, microsecond=0, tzinfo=None)
        if (not 1 <= len(name) <= 200 or len(room) > 100 or at.tzinfo is not None
                or (phone is not None and (not isinstance(phone, str) or len(phone) > 80))):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError('Укажите имя гостя, корректные дату и время съёмки.') from exc
    packages = list(await session.scalars(select(Package).where(Package.active.is_(True)).order_by(Package.id)))
    pid = guest.get('packageId')
    if pid is not None:
        if type(pid) is not int or not any(p.id == pid for p in packages):
            raise ValueError('Пакет больше не доступен. Обновите данные.')
        quote = await booking_unit_quote(session, hid, pid)
    else:
        candidates = []
        for package in packages:
            try:
                candidates.append((package.id, await booking_unit_quote(session, hid, package.id)))
            except ValueError:
                continue
        if not candidates or len({q['price'] for _, q in candidates}) != 1:
            raise ValueError('Выберите пакет или настройте фиксированную цену кадра в отеле.')
        pid, quote = candidates[0]
    quote |= {'inlineSale': True, 'managerCommissionEligible': 'MANAGER' in roles}
    client = Client(name=name, phone=phone)
    session.add(client)
    await session.flush()
    booking = Booking(hotel_id=hid, client_id=client.id, room=room, guest_count=1,
        deposit=0, shoot_date=day, shoot_time=at, package_id=pid,
        pricing_snapshot=json.dumps(quote, ensure_ascii=False),
        manager_id=actor.id, photographer_id=photographer_id, status='READY_FOR_SALE')
    session.add(booking)
    await session.flush()
    # This is an empty shooting record, never a fabricated full upload.
    session.add(Shooting(booking_id=booking.id, status='READY_FOR_SALE'))
    await audit(session, actor, 'sale_inline_booking_created', 'booking', booking.id)
    return await start_sale_draft(session, actor, roles, booking.id)
