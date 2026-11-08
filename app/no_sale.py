"""«Гость не купил»: an explicit outcome after a completed shoot.

The booking keeps READY_FOR_SALE, so a guest who comes back can still buy;
a later completed sale clears the mark. Nothing financial is created or
changed here. Money already involved (a payment receipt, a QR payment for a
purchase) always blocks the mark: such cases need the owner, not a button.
"""
import json
from datetime import UTC, datetime

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import BankPayment, Booking, Receipt, Sale, User
from .services.core import audit, roles_of
from .services.sale_workflow import active_draft, can_sell

REASONS = {
    'NOT_LIKED': 'Не понравились фотографии',
    'PRICE': 'Дорого',
    'NO_MONEY': 'Нет денег с собой',
    'LATER': 'Подумает, вернётся позже',
    'OTHER': 'Другая причина',
}
NOTE_LIMIT = 500
# Purchase money that is still moving or already received for this booking.
OPEN_BANK_STATUSES = frozenset({'CREATING', 'PENDING', 'RECONCILIATION_REQUIRED', 'PAID', 'TEST_PAID', 'PARTIAL_REFUND'})


def public(booking):
    """The mark as shown to staff, or None."""
    if booking is None or booking.no_sale_at is None:
        return None
    return {'reason': booking.no_sale_reason, 'label': REASONS.get(booking.no_sale_reason, 'Причина не указана'),
            'note': booking.no_sale_note or '', 'at': booking.no_sale_at.isoformat() + 'Z',
            'byId': booking.no_sale_by_id}


def public_row(reason, note, at, by_id=None):
    """Same shape for raw SQL rows."""
    if at is None:
        return None
    stamp = at if isinstance(at, str) else at.isoformat()
    return {'reason': reason, 'label': REASONS.get(reason, 'Причина не указана'), 'note': note or '',
            'at': stamp + ('' if stamp.endswith('Z') else 'Z'), 'byId': by_id}


def clear(booking):
    """Called when a sale is completed after all. Returns the cleared mark."""
    previous = public(booking)
    booking.no_sale_reason = booking.no_sale_note = None
    booking.no_sale_at = booking.no_sale_by_id = None
    return previous


async def _money_in_progress(session, booking, draft):
    if draft is not None and draft.receipt_file_id:
        return 'По этой продаже уже прикреплён чек. Завершите продажу или обратитесь к владельцу.'
    if draft is not None and '_bankQuote' in (draft.pricing_snapshot or ''):
        return 'По этой продаже уже выставлен счёт по QR. Проверьте оплату у владельца.'
    receipt = await session.scalar(select(Receipt.id).where(
        Receipt.booking_id == booking.id, Receipt.purpose == 'PAYMENT',
        Receipt.status.in_(['PENDING', 'APPROVED'])).limit(1))
    if receipt:
        return f'По съёмке есть чек оплаты №{receipt}. Решение принимает владелец.'
    for bill in await session.scalars(select(BankPayment).where(
            BankPayment.booking_id == booking.id, BankPayment.status.in_(OPEN_BANK_STATUSES))):
        try:
            kind = json.loads(bill.pricing_snapshot or '{}').get('kind')
        except ValueError:
            kind = None
        if kind != 'DEPOSIT':
            return 'По съёмке есть оплата по QR. Решение принимает владелец.'
    return None


async def mark(session, actor, roles, booking_id, reason, note=''):
    booking = await session.get(Booking, booking_id, with_for_update=True)
    if booking is None or not await can_sell(actor, roles, booking, session=session):
        raise AccessError('Нет доступа к этой съёмке или она ещё не завершена.', 403)
    if reason not in REASONS:
        raise AccessError('Выберите причину.', 400)
    if not isinstance(note, str) or len(note) > NOTE_LIMIT:
        raise AccessError(f'Комментарий — до {NOTE_LIMIT} символов.', 400)
    note = ' '.join(note.split())
    if reason == 'OTHER' and not note:
        raise AccessError('Напишите коротко, почему гость не купил.', 400)
    if booking.status != 'READY_FOR_SALE' or await session.scalar(
            select(Sale.id).where(Sale.booking_id == booking.id).limit(1)):
        raise AccessError('По этой съёмке уже оформлена продажа.', 409)
    draft = await active_draft(session, booking.id)
    blocker = await _money_in_progress(session, booking, draft)
    if blocker:
        raise AccessError(blocker, 409)
    if draft is not None:
        if draft.created_by_id != actor.id and not {'OWNER', 'ADMIN'} & roles:
            owner = await session.get(User, draft.created_by_id)
            raise AccessError(f'Продажу оформляет {owner.name if owner else "другой сотрудник"}. '
                              'Отметить «не купил» может он, администратор или владелец.', 409)
        # A started sale without any money is simply abandoned; uploaded files stay stored.
        draft.status = 'CANCELLED'
        await audit(session, actor, 'sale_draft_cancelled', 'sale_draft', draft.id, 'reason=no_sale')
    already = booking.no_sale_at is not None
    booking.no_sale_reason, booking.no_sale_note = reason, note or None
    booking.no_sale_at = datetime.now(UTC).replace(tzinfo=None)
    booking.no_sale_by_id = actor.id
    await audit(session, actor, 'booking_no_sale', 'booking', booking.id,
                json.dumps({'reason': reason, 'label': REASONS[reason], 'note': note, 'replaced': already},
                           ensure_ascii=False))
    return public(booking)


async def unmark(session, actor, roles, booking_id):
    booking = await session.get(Booking, booking_id, with_for_update=True)
    if booking is None or not await can_sell(actor, roles, booking, session=session):
        raise AccessError('Нет доступа к этой съёмке.', 403)
    if booking.no_sale_at is None:
        return None
    previous = clear(booking)
    await audit(session, actor, 'booking_no_sale_cleared', 'booking', booking.id,
                json.dumps(previous, ensure_ascii=False))
    return previous


class NoSale:
    def __init__(self, api):
        self.api = api

    async def _actor(self, session, request):
        a = request['miniapp_actor']
        actor = await session.get(User, a['id'])
        roles = (await roles_of(session, actor)) & set(a['roles']) if actor else set()
        if not actor or not roles:
            raise AccessError('Нет доступа.', 403)
        return actor, roles

    async def post(self, request):
        body = await self.api.body(request)
        if set(body) - {'reason', 'note'} or 'reason' not in body:
            raise AccessError('Некорректный запрос.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            actor, roles = await self._actor(session, request)
            result = await mark(session, actor, roles, int(request.match_info['id']),
                                body['reason'], body.get('note', ''))
            await session.commit()
        return web.json_response({'bookingId': int(request.match_info['id']), 'noSale': result})

    async def delete(self, request):
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            actor, roles = await self._actor(session, request)
            previous = await unmark(session, actor, roles, int(request.match_info['id']))
            await session.commit()
        return web.json_response({'bookingId': int(request.match_info['id']), 'noSale': None,
                                  'cleared': previous is not None})


def install_no_sale(app, api):
    service = NoSale(api)
    app.router.add_post(r'/api/miniapp/bookings/{id:\d+}/no-sale', service.post)
    app.router.add_delete(r'/api/miniapp/bookings/{id:\d+}/no-sale', service.delete)
