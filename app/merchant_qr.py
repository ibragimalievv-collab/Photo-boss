"""Read-only display of a bank-issued, permanent merchant QR.

This is deliberately separate from BankPayment: a picture/link is neither an
order-specific bank invoice nor evidence that any money has arrived. Only the
existing verified receipt ledger determines the amount still due.
"""
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import BankPayment, Booking, Receipt, Sale, SaleDraft, User
from .payment_mode import receipts_only, require_payment_qr
from .sber_qr_client import is_payment_url
from .services.booking_access import can_access_booking
from .services.core import roles_of
from .services.pricing import sale_quote
from .services.receipts import money, payment_totals
from .services.sale_workflow import active_draft, can_sell, validate_remaining_photos

_NO_STORE = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
             'Referrer-Policy': 'no-referrer'}
_UNAVAILABLE = 'QR для оплаты ещё не настроен. Обратитесь к сотруднику.'
_MAX_IMAGE_BYTES = 1024 * 1024


def _private_file(name, limit):
    value = os.getenv(name, '')
    if not value or not Path(value).is_absolute():
        raise ValueError
    path = Path(value)
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError
    with path.open('rb') as stream:
        result = stream.read(limit + 1)
    if not 0 < len(result) <= limit:
        raise ValueError
    return result


def merchant_qr_asset():
    """Fail closed on an invalid/missing private source; never log its content."""
    require_payment_qr()
    try:
        raw = _private_file('SBER_MERCHANT_QR_FILE', _MAX_IMAGE_BYTES)
        if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError
        with Image.open(io.BytesIO(raw)) as image:
            if (image.format != 'PNG' or image.width != image.height
                    or not 64 <= image.width <= 2048 or getattr(image, 'n_frames', 1) != 1):
                raise ValueError
            image.verify()
        url = _private_file('SBER_MERCHANT_QR_URL_FILE', 512).decode('utf-8').strip()
        if (not is_payment_url(url) or urlsplit(url).hostname != 'qr.nspk.ru'
                or urlsplit(url).query):
            raise ValueError
        return raw, url
    except (OSError, ValueError, SyntaxError, UnicodeError, Image.DecompressionBombError):
        raise AccessError(_UNAVAILABLE, 503) from None


def merchant_qr_settings():
    if receipts_only():
        return {'merchantQRConfigured': False, 'merchantQRAutomaticConfirmation': False}
    try:
        merchant_qr_asset()
        available = True
    except AccessError:
        available = False
    return {'merchantQRConfigured': available, 'merchantQRAutomaticConfirmation': False}


def _preview_body(body):
    required = {'bookingId', 'total', 'sold', 'tariffId', 'amount', 'requestKey', 'purpose'}
    optional = {'discountId', 'albumId', 'albumQuantity', 'sourceDraftId', 'allowZeroDue', 'photoAmount'}
    if (not isinstance(body, dict) or set(body) - optional != required
            or type(body['bookingId']) is not int or not 0 < body['bookingId'] < 2**31
            or not isinstance(body['purpose'], str)
            or body['purpose'] not in {'DEPOSIT', 'PURCHASE', 'BALANCE'}
            or not isinstance(body['requestKey'], str) or not 16 <= len(body['requestKey']) <= 80
            or type(body['total']) is not int or not 0 <= body['total'] <= 10000
            or type(body['sold']) is not int or not 0 <= body['sold'] <= body['total']
            or (body['purpose'] == 'PURCHASE' and body['sold'] < 1)
            or any(body.get(key) is not None and
                   (type(body[key]) is not int or body[key] <= 0)
                   for key in ('tariffId', 'discountId', 'albumId', 'sourceDraftId'))
            or (body.get('albumQuantity') is not None and
                (type(body['albumQuantity']) is not int or not 1 <= body['albumQuantity'] <= 100))
            or ('allowZeroDue' in body and type(body['allowZeroDue']) is not bool)):
        raise AccessError('Выберите съёмку, тариф и количество кадров.', 400)
    return body


async def merchant_quote(session, actor, roles, booking, body):
    """Use the same locked ledger and tariff calculation as bank invoices.

    A preview does not freeze a quote, accept counts, or create an invoice.
    Completed-draft retries quote the existing balance instead of the sale twice.
    """
    previous, paid, _ = await payment_totals(session, booking.id)
    purpose = body['purpose']
    if purpose == 'DEPOSIT':
        deposits = await session.scalar(select(func.coalesce(func.sum(Receipt.verified_amount), 0)).where(
            Receipt.booking_id == booking.id, Receipt.purpose == 'DEPOSIT', Receipt.status == 'APPROVED'))
        due = max(money(booking.deposit) - money(deposits), money(0))
    elif purpose == 'BALANCE':
        due = max(money(previous) - money(paid), money(0))
    else:
        source_id = body.get('sourceDraftId')
        draft = await session.get(SaleDraft, source_id, with_for_update=True,
            populate_existing=True) if source_id else await active_draft(session, booking.id)
        if source_id and (not draft or draft.booking_id != booking.id or draft.status == 'CANCELLED'):
            raise ValueError('Черновик оплаты недоступен в этой съёмке. Обновите продажу.')
        if draft and draft.created_by_id != actor.id:
            raise ValueError('Эту продажу уже оформляет другой сотрудник.')
        if not await can_sell(actor, roles, booking, session=session):
            raise ValueError('Эта запись ещё не готова к продаже или недоступна.')
        if draft and draft.status == 'COMPLETED':
            if not await session.scalar(select(Sale.id).where(
                    Sale.source_draft_id == draft.id, Sale.booking_id == booking.id)):
                raise ValueError('Не найдена оформленная продажа для этой оплаты.')
            purpose = 'BALANCE'
            due = max(money(previous) - money(paid), money(0))
        else:
            if previous > 0 and not draft:
                raise ValueError('Для оформленной продажи выберите остаток. Для новой покупки откройте её черновик.')
            frozen = draft.pricing_snapshot if draft else None
            recorded = json.loads(frozen) if frozen else {}
            if frozen and (recorded.get('sold', draft.sold_photos) != body['sold']
                    or recorded.get('total', draft.declared_photo_count) != body['total']):
                raise ValueError('Количество кадров не совпадает с сохранённым расчётом продажи.')
            if recorded.get('_bankQuote') and body.get('discountId') not in (None, recorded.get('discountId')):
                raise ValueError('Скидка уже закреплена в банковском счёте.')
            await validate_remaining_photos(session, booking, body['total'], body['sold'])
            quote = await sale_quote(session, booking, body['sold'], body['total'], body['tariffId'],
                saved=frozen, discount_id=body.get('discountId'), album_id=body.get('albumId'),
                album_quantity=body.get('albumQuantity'), photo_amount=body.get('photoAmount'))
            due = max(money(previous) + money(quote['amount']) - money(paid), money(0))
    amount = due if body['amount'] is None else money(body['amount'])
    if amount < 0 or amount > due or (due > 0 and amount == 0):
        raise ValueError('Сумма оплаты должна быть положительной и не превышать остаток.')
    return purpose, amount, due, money(paid)


class MerchantQR:
    def __init__(self, api):
        self.api = api

    async def _booking(self, session, request, bid):
        actor_id = request.get('miniapp_actor', {}).get('id')
        actor = await session.get(User, actor_id) if type(actor_id) is int else None
        roles = await roles_of(session, actor) if actor and actor.active else set()
        booking = await session.get(Booking, bid, with_for_update=True)
        if not await can_access_booking(session, actor_id, roles, booking):
            raise AccessError('Нет доступа к этой съёмке.', 403)
        if booking.status in {'CANCELLED', 'REJECTED'}:
            raise AccessError('Отменённую съёмку оплачивать нельзя.', 409)
        pending_real = await session.scalar(select(BankPayment.id).where(
            BankPayment.booking_id == booking.id, BankPayment.test_mode.is_(False),
            BankPayment.status.in_(['CREATING', 'PENDING', 'RECONCILIATION_REQUIRED'])).limit(1))
        if pending_real:
            # The bank may have accepted an order despite an uncertain response.
            # Never offer a second payment route while a real invoice is pending.
            # A sandbox order cannot receive/credit real money and is independent.
            raise AccessError('По съёмке уже есть банковский счёт. Сначала проверьте его статус.', 409)
        return actor, roles, booking

    async def preview(self, request):
        require_payment_qr()
        body = _preview_body(await self.api.body(request))
        raw, url = merchant_qr_asset()
        async with AsyncSession(self.api.engine) as session:
            actor, roles, booking = await self._booking(session, request, body['bookingId'])
            try:
                # sale_quote can recover a legacy booking snapshot in memory.
                # Suppress autoflush and close without commit: display is read-only.
                with session.no_autoflush:
                    purpose, amount, due, paid = await merchant_quote(session, actor, roles, booking, body)
            except (ValueError, ArithmeticError, TypeError, KeyError) as exc:
                raise AccessError(str(exc), 400) from exc
        positive = amount > 0
        return web.json_response({'bookingId': body['bookingId'], 'purpose': purpose,
            'amount': str(amount), 'dueAmount': str(due), 'paidAmount': str(paid),
            'status': 'AWAITING_BANK_CONFIRMATION' if positive else 'NO_PAYMENT_REQUIRED',
            'qrPath': f'/merchant-qr/{body["bookingId"]}/qr' if positive else None,
            'paymentUrl': url if positive else None,
            'qrSHA256': hashlib.sha256(raw).hexdigest() if positive else None,
            'confirmation': 'BANK_API_NOT_CONNECTED', 'automaticConfirmation': False}, headers=_NO_STORE)

    async def qr(self, request):
        require_payment_qr()
        value = request.match_info.get('booking', '')
        if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**31:
            raise AccessError('Выберите съёмку.', 400)
        async with AsyncSession(self.api.engine) as session:
            await self._booking(session, request, int(value))
        raw, _ = merchant_qr_asset()
        return web.Response(body=raw, content_type='image/png', headers=_NO_STORE)


def install_merchant_qr(app, api):
    service = MerchantQR(api)
    app.router.add_post('/api/miniapp/merchant-qr/preview', service.preview)
    app.router.add_get(r'/api/miniapp/merchant-qr/{booking:\d+}/qr', service.qr)
