"""Scoped booking corrections, cancellation and recoverable removal."""
import hashlib
import json
from datetime import date, time

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import Booking, Client, Sale, Setting, Shooting, User, utc_now
from .people import positive_id
from .services.booking_access import can_access_booking
from .services.core import audit, roles_of


def version(values):
    keys = ('id', 'room', 'date', 'time', 'status', 'client', 'phone', 'guestCount',
            'photographerId', 'shootingStatus')
    return hashlib.sha256(json.dumps({k: values.get(k) for k in keys},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def snapshot(booking, client, shooting):
    return {'id': booking.id, 'room': booking.room, 'date': str(booking.shoot_date),
            'time': str(booking.shoot_time)[:5], 'status': booking.status,
            'client': client.name, 'phone': client.phone, 'guestCount': booking.guest_count,
            'photographerId': booking.photographer_id,
            'shootingStatus': shooting.status if shooting else None}


class BookingManagement:
    def __init__(self, api):
        self.api = api

    async def manage(self, request):
        actor = request['miniapp_actor']
        bid, body = positive_id(request.match_info['id']), await self.api.body(request)
        mode = body.get('mode')
        if mode not in ('edit', 'cancel', 'trash', 'restore'):
            raise AccessError('Выберите действие с записью.', 400)
        required = {'mode', 'revision', 'reason'} | ({'client', 'phone', 'room', 'guestCount', 'date', 'time'} if mode == 'edit' else set())
        if set(body) != required or not isinstance(body.get('revision'), str):
            raise AccessError('Обновите карточку записи перед изменением.', 400)
        reason = body.get('reason')
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
            raise AccessError('Укажите причину изменения от 3 до 500 символов.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            user = await session.get(User, actor['id'])
            roles = await roles_of(session, user) if user and user.active else set()
            b = await session.get(Booking, bid, with_for_update=True)
            if not roles & {'OWNER', 'ADMIN', 'MANAGER'} or not await can_access_booking(session, actor['id'], roles, b):
                raise AccessError('Запись недоступна для изменения.', 403)
            # A photographer's assignment alone never grants booking-editor access.
            if not roles & {'OWNER', 'ADMIN'} and b.manager_id != actor['id']:
                raise AccessError('Можно изменять только собственные записи.', 403)
            client = await session.get(Client, b.client_id)
            shoot = await session.scalar(select(Shooting).where(Shooting.booking_id == bid).with_for_update())
            before = snapshot(b, client, shoot)
            if version(before) != body['revision']:
                raise AccessError('Запись уже изменена. Обновите карточку и повторите.', 409)
            key = f'booking:trash:{bid}'
            trash = await session.get(Setting, key)
            sale = await session.scalar(select(Sale.id).where(Sale.booking_id == bid).limit(1))
            started = bool(shoot and (shoot.started_at or shoot.status in {
                'SHOOTING', 'SHOT', 'READY_FOR_MANAGER', 'READY_FOR_SALE', 'COMPLETED', 'SOLD'}))
            if mode == 'edit':
                if trash or b.status in {'CANCELLED', 'REJECTED'}:
                    raise AccessError('Сначала восстановите отменённую запись.', 409)
                try:
                    name, phone, room = body['client'].strip(), body['phone'].strip(), body['room'].strip()
                    day, at = date.fromisoformat(body['date']), time.fromisoformat(body['time'])
                    if (not 2 <= len(name) <= 200 or not 1 <= len(room) <= 100 or
                            (phone and not 5 <= len(phone) <= 80) or at.tzinfo or
                            type(body['guestCount']) is not int or not 1 <= body['guestCount'] <= 100):
                        raise ValueError
                except (ValueError, TypeError, AttributeError):
                    raise AccessError('Проверьте имя, телефон, комнату, дату и количество гостей.', 400) from None
                moved = day != b.shoot_date or at != b.shoot_time
                if moved and (sale or started):
                    raise AccessError('Начавшуюся или проданную съёмку переносить нельзя. Контактные данные можно исправить.', 409)
                if moved and day < self.api.today():
                    raise AccessError('Новая дата съёмки не может быть в прошлом.', 400)
                # Never change another booking that happens to use the same client.
                if name != client.name or (phone or None) != client.phone:
                    client = Client(name=name, phone=phone or None)
                    session.add(client)
                    await session.flush()
                    b.client_id = client.id
                b.room, b.guest_count, b.shoot_date, b.shoot_time = room, body['guestCount'], day, at
                if moved:
                    b.status = 'RESCHEDULED'
                    if shoot:
                        shoot.status = 'ASSIGNED' if b.photographer_id else 'PENDING_CONFIRMATION'
                action = 'booking_rescheduled' if moved else 'booking_updated'
            elif mode in ('cancel', 'trash'):
                if sale or started:
                    raise AccessError('У съёмки уже есть работа или продажа. Удаление недоступно; финансовая история сохраняется.', 409)
                b.status, b.cancellation_reason, b.cancelled_at = 'CANCELLED', reason.strip(), utc_now()
                if shoot:
                    shoot.status = 'CANCELLED'
                if mode == 'trash' and not trash:
                    session.add(Setting(key=key, value=json.dumps({'at': utc_now().isoformat(), 'reason': reason.strip()})))
                action = 'booking_trashed' if mode == 'trash' else 'booking_cancelled'
            else:
                if sale or started:
                    raise AccessError('Восстановление этой съёмки требует проверки владельцем.', 409)
                if trash:
                    await session.delete(trash)
                b.status, b.cancellation_reason, b.cancelled_at = ('ASSIGNED' if b.photographer_id else 'PENDING_CONFIRMATION'), None, None
                if shoot:
                    shoot.status = b.status
                action = 'booking_restored'
            await audit(session, user, action, 'booking', b.id, json.dumps({
                'before': before, 'after': snapshot(b, client, shoot), 'reason': reason.strip()}, ensure_ascii=False))
            await session.commit()
        return web.json_response({'ok': True, 'bookingId': bid})


def install_booking_management(app, api):
    service = BookingManagement(api)
    app.router.add_post('/api/miniapp/bookings/{id}/manage', service.manage)
    return service
