"""Explicit, scoped approval of a 50% discount for the entire photo shoot."""
import json
from decimal import Decimal

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..miniapp_security import AccessError
from ..models import Booking, Sale, SaleDiscountApproval, User, utc_now
from .booking_access import can_access_booking
from .core import audit, roles_of
from .receipts import money


def public_approval(row):
    return {'id': row.id, 'bookingId': row.booking_id, 'total': row.total_photos,
            'unitPrice': str(money(row.unit_price)), 'percent': 50, 'status': row.status,
            'amount': str(money(row.unit_price * row.total_photos / 2)),
            'requestedBy': row.requested_by_id, 'reviewedBy': row.reviewed_by_id}


async def request_discount(session, actor, booking_id, total):
    from .pricing import sale_quote
    from .sale_workflow import can_sell, validate_remaining_photos
    booking = await session.get(Booking, booking_id, with_for_update=True)
    roles = await roles_of(session, actor)
    if not await can_sell(actor, roles, booking, session=session):
        raise AccessError('Нет доступа к продаже этой съёмки.', 403)
    if type(total) is not int or not 1 <= total <= 10000:
        raise AccessError('Укажите целое количество всех кадров съёмки.', 400)
    if await session.scalar(select(Sale.id).where(Sale.booking_id == booking.id).limit(1)):
        raise AccessError('Скидка на всю съёмку недоступна после частичной продажи.', 409)
    await validate_remaining_photos(session, booking, total, total)
    quote = await sale_quote(session, booking, total, total)
    if quote['kind'] != 'UNIT':
        raise AccessError('Скидка рассчитывается от цены всех кадров, отдельно от пакетов и альбомов.', 409)
    row = await session.scalar(select(SaleDiscountApproval).where(
        SaleDiscountApproval.booking_id == booking.id,
        SaleDiscountApproval.total_photos == total,
        SaleDiscountApproval.unit_price == money(quote['price'])))
    if row is not None:
        return row
    row = SaleDiscountApproval(booking_id=booking.id, requested_by_id=actor.id,
                              total_photos=total, unit_price=money(quote['price']))
    session.add(row)
    await session.flush()
    from ..push_notifications import queue_notice, scoped_supervisors
    from .booking_notices import booking_description
    description = await booking_description(session, booking)
    for uid in await scoped_supervisors(session, booking.hotel_id):
        await queue_notice(session, uid, kind='sale_discount', event_key=f'sale-discount:{row.id}:requested',
            url=f'/app/#workflow?booking={booking.id}',
            message=f'Запрос скидки 50% на всю съёмку: {total} кадров, {public_approval(row)["amount"]} ₽.\n{description}',
            payload={'bookingId': booking.id, 'discountId': row.id})
    await audit(session, actor, 'sale_discount_requested', 'sale_discount_approval', row.id,
                json.dumps(public_approval(row), ensure_ascii=False))
    return row


async def decide_discount(session, actor, approval_id, decision):
    preview = await session.get(SaleDiscountApproval, approval_id)
    booking = await session.get(Booking, preview.booking_id, with_for_update=True) if preview else None
    row = await session.get(SaleDiscountApproval, approval_id, with_for_update=True, populate_existing=True) if preview else None
    roles = await roles_of(session, actor)
    if (not row or not roles & {'OWNER', 'ADMIN'}
            or not await can_access_booking(session, actor.id, roles, booking)):
        raise AccessError('Согласовать скидку может владелец или администратор этого отеля.', 403)
    if not isinstance(decision, str) or decision not in {'APPROVED', 'REJECTED'}:
        raise AccessError('Выберите подтверждение или отказ.', 400)
    if row.status != 'PENDING':
        if row.status != decision:
            raise AccessError('По этому запросу уже принято другое решение.', 409)
        return row
    if await session.scalar(select(Sale.id).where(Sale.booking_id == booking.id).limit(1)):
        raise AccessError('По съёмке уже есть продажа; запрос больше нельзя согласовать.', 409)
    row.status, row.reviewed_by_id, row.reviewed_at = decision, actor.id, utc_now()
    from ..push_notifications import queue_notice
    await queue_notice(session, row.requested_by_id, kind='sale_discount',
        event_key=f'sale-discount:{row.id}:decided', url=f'/app/#workflow?booking={booking.id}',
        message=f'Скидка 50% на всю съёмку №{booking.id}: {"подтверждена" if decision == "APPROVED" else "отклонена"}.',
        payload={'bookingId': booking.id, 'discountId': row.id})
    await audit(session, actor, 'sale_discount_decided', 'sale_discount_approval', row.id, decision)
    return row


async def apply_discount(session, booking, quote, discount_id=None, saved=None):
    """Called before album extras; approval is rechecked on every final sale."""
    frozen = json.loads(saved) if saved else {}
    recorded_id = frozen.get('discountId')
    if recorded_id and discount_id not in (None, recorded_id):
        raise ValueError('Скидка продажи уже зафиксирована.')
    approval_id = recorded_id or discount_id
    if approval_id is None:
        return quote
    if type(approval_id) is not int or approval_id <= 0:
        raise ValueError('Некорректный запрос скидки.')
    row = await session.get(SaleDiscountApproval, approval_id)
    if (not row or row.booking_id != booking.id or row.status != 'APPROVED'
            or quote['kind'] != 'UNIT' or quote['sold'] != quote['total']
            or row.total_photos != quote['total'] or money(row.unit_price) != money(quote['price'])):
        raise ValueError('Для этой цены и всех кадров требуется подтверждённая скидка руководителя.')
    if await session.scalar(select(Sale.id).where(Sale.booking_id == booking.id).limit(1)):
        raise ValueError('Скидка на всю съёмку недоступна после частичной продажи.')
    subtotal = money(Decimal(quote['sold']) * money(quote['price']))
    amount = money(subtotal / 2)
    return quote | {'discountId': row.id, 'discountPercent': 50, 'subtotal': str(subtotal),
                    'amount': str(amount), 'lines': quote['lines'] + [{
                        'type': 'DISCOUNT', 'name': 'Скидка 50% на всю съёмку, согласована',
                        'price': str(-money(subtotal - amount)), 'quantity': 1,
                        'amount': str(-money(subtotal - amount))}]}


class SaleDiscounts:
    def __init__(self, api):
        self.api = api

    async def request(self, request):
        body = await self.api.body(request)
        if set(body) != {'bookingId', 'total'} or type(body['bookingId']) is not int:
            raise AccessError('Проверьте съёмку и количество кадров.', 400)
        return await self._apply(request, lambda session, actor: request_discount(
            session, actor, body['bookingId'], body['total']))

    async def decide(self, request):
        body = await self.api.body(request)
        raw_id = request.match_info.get('id', '')
        if set(body) != {'decision'} or not raw_id.isascii() or not raw_id.isdigit():
            raise AccessError('Проверьте запрос скидки.', 400)
        return await self._apply(request, lambda session, actor: decide_discount(
            session, actor, int(raw_id), body['decision']))

    async def _apply(self, request, operation):
        # HTTP mutation middleware already holds the reset barrier.
        async with AsyncSession(self.api.engine) as session:
            actor = await session.get(User, request['miniapp_actor']['id'], with_for_update=True)
            if not actor or not actor.active:
                raise AccessError('Пользователь недоступен.', 403)
            try:
                row = await operation(session, actor)
                result = public_approval(row)
                await session.commit()
            except AccessError:
                raise
            except ValueError as exc:
                raise AccessError(str(exc), 409) from exc
        return web.json_response({'ok': True, 'discount': result})
