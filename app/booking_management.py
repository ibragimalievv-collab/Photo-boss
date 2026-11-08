"""Scoped booking corrections, cancellation and recoverable removal."""
import hashlib
import json
from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import Booking, Client, Receipt, Sale, Setting, Shooting, User, utc_now
from .payment_mode import receipts_only
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


def first_cancellation_notice(raw, notice_id, booking, now):
    try:
        notice = json.loads(raw)
        received = datetime.fromisoformat(notice['receivedAtUTC'])
        day, at = date.fromisoformat(notice['shootDate']), time.fromisoformat(notice['shootTime'])
        reason, kind = notice['reason'], notice['kind']
        if (type(notice['version']) is not int or notice['version'] != 1 or type(notice['bookingId']) is not int
                or notice['bookingId'] != booking.id or notice['noticeId'] != notice_id
                or str(UUID(notice_id)) != notice_id or notice['bankRefundConfirmed'] is not False
                or received.tzinfo is None or received.utcoffset().total_seconds() != 0
                or received > now.replace(tzinfo=UTC) or at.tzinfo is not None
                or day != booking.shoot_date or at != booking.shoot_time
                or type(notice['actorId']) is not int or notice['actorId'] <= 0
                or not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500
                or not isinstance(kind, str) or kind not in {'ordinary', 'force_majeure'}):
            raise ValueError
        category = ('FORCE_MAJEURE' if kind == 'force_majeure' else
                    'EARLY' if received.astimezone(ZoneInfo('Europe/Moscow')).date() < day else 'LATE')
        if notice['category'] != category:
            raise ValueError
        return reason, received.replace(tzinfo=None)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AccessError('История отмены требует сверки владельца.', 409) from None


class BookingManagement:
    def __init__(self, api):
        self.api = api

    async def manage(self, request):
        received_at = datetime.now(UTC)
        actor = request['miniapp_actor']
        bid, body = positive_id(request.match_info['id']), await self.api.body(request)
        mode = body.get('mode')
        if mode not in ('edit', 'cancel', 'trash', 'restore'):
            raise AccessError('Выберите действие с записью.', 400)
        required = {'mode', 'revision', 'reason'} | ({'client', 'phone', 'room', 'guestCount', 'date', 'time'} if mode == 'edit' else set())
        native_receipts = bool(getattr(self.api, 'standalone', False) and receipts_only())
        integration = getattr(self.api, 'booking_refunds', None)
        optional = {'cancellationKind'} if (native_receipts or integration is not None) and mode in ('cancel', 'trash') else set()
        if integration is not None and mode in ('cancel', 'trash'):
            optional.add('reservationVersionId')
        if set(body) - optional != required or not isinstance(body.get('revision'), str):
            raise AccessError('Обновите карточку записи перед изменением.', 400)
        if (not isinstance(body.get('cancellationKind', 'ordinary'), str)
                or body.get('cancellationKind', 'ordinary') not in {'ordinary', 'force_majeure'}):
            raise AccessError('Выберите обычную отмену или форс-мажор.', 400)
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
            integrated_cancel = integration is not None and mode in ('cancel', 'trash')
            if not integrated_cancel and version(before) != body['revision']:
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
                    if (phone or None) != client.phone:
                        from .services.guest_phone import guest_phone
                        phone = guest_phone(phone) or ''
                    day, at = date.fromisoformat(body['date']), time.fromisoformat(body['time'])
                    if (not 2 <= len(name) <= 200 or not 1 <= len(room) <= 100 or
                            (phone and not 5 <= len(phone) <= 80) or at.tzinfo or
                            type(body['guestCount']) is not int or not 1 <= body['guestCount'] <= 100):
                        raise ValueError
                except (ValueError, TypeError, AttributeError):
                    raise AccessError('Проверьте имя, телефон, комнату, дату и количество гостей.', 400) from None
                from .operational_start import (
                    OperationalStartError,
                    require_booking_date,
                )
                try:
                    require_booking_date(day)
                except OperationalStartError as error:
                    raise AccessError(str(error), 409) from None
                moved = day != b.shoot_date or at != b.shoot_time
                if moved and (sale or started):
                    raise AccessError('Начавшуюся или проданную съёмку переносить нельзя. Контактные данные можно исправить.', 409)
                if moved and day < self.api.today():
                    raise AccessError('Новая дата съёмки не может быть в прошлом.', 400)
                if moved and integration is not None:
                    from .services.booking_reservations import ReservationConflict, append_slot_version
                    try:
                        reserved = await append_slot_version(session, b, actor['id'],
                            active_version_id=b.reservation_version_id, shoot_date=day,
                            shoot_time=at, reason=reason, recorded_at=received_at)
                    except ReservationConflict:
                        raise AccessError('Перенос требует проверки неизменяемой истории брони.', 409) from None
                    b.reservation_version_id = reserved.id
                    await integration.bump_version(session, b)
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
                if integration is not None:
                    from .bank_refunds import RefundError
                    from .services.booking_reservations import ReservationConflict
                    try:
                        notice = await integration.cancel_booking(session, b, actor['id'],
                            revision=body['revision'], current_revision=version(before), reason=reason,
                            force_majeure=body.get('cancellationKind', 'ordinary') == 'force_majeure',
                            received_at=received_at, shooting=shoot, has_sale=bool(sale), mode=mode,
                            requested_version_id=body.get('reservationVersionId'),
                            requested_version_present='reservationVersionId' in body)
                    except ReservationConflict as error:
                        message = ('Запись уже изменена. Обновите карточку и повторите.'
                            if str(error) == 'booking_revision_changed' else
                            'Отмена требует проверки истории брони, прав или выполненной работы.')
                        raise AccessError(message, 409) from None
                    except RefundError:
                        raise AccessError('Деньги по записи требуют банковской сверки. Состояние возврата не подтверждено.', 409) from None
                    if getattr(notice, 'stale', False):
                        result = await integration.public_status(session, b)
                        await session.commit()
                        return web.json_response({'error': 'Время обращения сохранено. Запись изменилась; нужна проверка отмены.',
                            'bookingId': bid, 'reviewRequired': True, 'refund': result}, status=409)
                    from .booking_reservation_models import BookingReservationVersion
                    active = await session.get(BookingReservationVersion, b.reservation_version_id) if b.reservation_version_id else None
                    same_reservation = ((active.reservation_key if active else None) == notice.case.reservation_key)
                    changed_trash = mode == 'trash' and not trash and b.status == 'CANCELLED' and same_reservation
                    if changed_trash:
                        session.add(Setting(key=key, value=json.dumps({'at': utc_now().isoformat(), 'reason': reason.strip()})))
                    if not notice.repeated or changed_trash:
                        await audit(session, user, 'booking_trashed' if changed_trash else 'booking_cancelled',
                            'booking', b.id, json.dumps({'before': before, 'after': snapshot(b, client, shoot),
                                'reason': notice.case.reason, 'cancellationCaseId': notice.case.id,
                                'refundState': notice.case.state}, ensure_ascii=False))
                        if not notice.repeated:
                            from .services.booking_notices import queue_photographer_change
                            await queue_photographer_change(session, b, 'cancelled')
                    result = await integration.public_status(session, b)
                    await session.commit()
                    return web.json_response({'ok': True, 'bookingId': bid,
                        'repeated': notice.repeated, 'refund': result})
                if sale or started:
                    raise AccessError('У съёмки уже есть работа или продажа. Удаление недоступно; финансовая история сохраняется.', 409)
                cancelled_at, first_reason = utc_now(), reason.strip()
                if native_receipts:
                    active_key = f'booking:cancel-active:{bid}'
                    active = await session.get(Setting, active_key)
                    if active is None:
                        notice_id = str(uuid4())
                        notice = {'version': 1, 'bookingId': bid, 'noticeId': notice_id,
                            'receivedAtUTC': cancelled_at.replace(tzinfo=UTC).isoformat(),
                            'actorId': actor['id'], 'reason': first_reason,
                            'shootDate': str(b.shoot_date), 'shootTime': str(b.shoot_time),
                            'revision': body['revision'], 'kind': body.get('cancellationKind', 'ordinary'),
                            'bankRefundConfirmed': False}
                        notice['category'] = ('FORCE_MAJEURE' if notice['kind'] == 'force_majeure' else
                            'EARLY' if cancelled_at.replace(tzinfo=UTC).astimezone(ZoneInfo('Europe/Moscow')).date() < b.shoot_date else 'LATE')
                        session.add_all([Setting(key=active_key, value=notice_id),
                            Setting(key='booking:cancel-notice:' + notice_id, value=json.dumps(notice, ensure_ascii=False))])
                    else:
                        saved = await session.get(Setting, 'booking:cancel-notice:' + active.value)
                        if saved is None:
                            raise AccessError('Не удалось проверить первое обращение об отмене. Нужна сверка владельца.', 409)
                        first_reason, cancelled_at = first_cancellation_notice(saved.value, active.value, b, cancelled_at)
                        if b.status == 'CANCELLED' and (mode == 'cancel' or trash):
                            return web.json_response({'ok': True, 'bookingId': bid, 'repeated': True})
                b.status, b.cancellation_reason, b.cancelled_at = 'CANCELLED', first_reason, cancelled_at
                if shoot:
                    shoot.status = 'CANCELLED'
                if mode == 'trash' and not trash:
                    session.add(Setting(key=key, value=json.dumps({'at': utc_now().isoformat(), 'reason': reason.strip()})))
                action = 'booking_trashed' if mode == 'trash' else 'booking_cancelled'
            else:
                if sale or started:
                    raise AccessError('Восстановление этой съёмки требует проверки владельцем.', 409)
                if integration is not None:
                    from .services.booking_reservations import ReservationConflict, append_slot_version
                    try:
                        reserved = await append_slot_version(session, b, actor['id'],
                            active_version_id=b.reservation_version_id, shoot_date=b.shoot_date,
                            shoot_time=b.shoot_time, reason=reason, recorded_at=received_at, restore=True)
                    except ReservationConflict:
                        raise AccessError('Восстановление требует новой даты или проверки истории брони.', 409) from None
                    b.reservation_version_id = reserved.id
                    await integration.bump_version(session, b)
                elif native_receipts:
                    money = await session.scalar(select(Receipt.id).where(Receipt.booking_id == bid,
                        Receipt.status.in_(['PENDING', 'APPROVED'])).limit(1))
                    if money:
                        raise AccessError('По отменённой записи есть чек или поступление. Создайте новую запись; '
                                          'прежняя предоплата остаётся на проверке владельца и не засчитывается повторно.', 409)
                    active = await session.get(Setting, f'booking:cancel-active:{bid}')
                    if active is not None:
                        # Only the current pointer is retired. The first notice
                        # remains in the audit archive and cannot be overwritten.
                        await session.delete(active)
                if trash:
                    await session.delete(trash)
                b.status, b.cancellation_reason, b.cancelled_at = ('ASSIGNED' if b.photographer_id else 'PENDING_CONFIRMATION'), None, None
                if shoot:
                    shoot.status = b.status
                action = 'booking_restored'
            await audit(session, user, action, 'booking', b.id, json.dumps({
                'before': before, 'after': snapshot(b, client, shoot), 'reason': reason.strip()}, ensure_ascii=False))
            if action in {'booking_rescheduled', 'booking_cancelled', 'booking_trashed'}:
                from .services.booking_notices import queue_photographer_change
                await queue_photographer_change(session, b,
                    'rescheduled' if action == 'booking_rescheduled' else 'cancelled')
            await session.commit()
        return web.json_response({'ok': True, 'bookingId': bid})


def install_booking_management(app, api):
    service = BookingManagement(api)
    app.router.add_post('/api/miniapp/bookings/{id}/manage', service.manage)
    return service
