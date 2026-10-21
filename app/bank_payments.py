"""Sber hosted payment pages; bank verification is the only automatic cash source.

No credentials or card details reach the browser. QR encodes the bank's hosted
page, not a fabricated SBP deep link. SBP availability depends on the merchant.
"""
import asyncio
import hashlib
import io
import json
import logging
import os
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp
import qrcode
from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import BankPayment, Booking, Hotel, Receipt, User, utc_now
from .services.booking_access import can_access_booking
from .services.core import audit, roles_of
from .services.pricing import sale_quote
from .services.receipts import money, payment_totals, refresh_payment_statuses

log = logging.getLogger(__name__)
GATEWAY = 'https://ecommerce.sberbank.ru/ecomm/gw/partner/api/v1/'


def configured():
    return os.getenv('SBER_PAYMENTS_ENABLED') == '1' and bool(os.getenv('SBER_MERCHANT_LOGIN') and os.getenv('SBER_MERCHANT_PASSWORD'))


def bank_url(value):
    parsed = urlparse(value or '')
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and parsed.hostname in {
                'ecommerce.sberbank.ru', 'ecomtest.sberbank.ru', 'ecomift.sberbank.ru',
                'securepayments.sberbank.ru', 'sbergate.com', 'payecom.ru'})


async def bank_call(method, payload):
    if not configured():
        raise AccessError('СберБизнес ещё не подключён. Владелец должен настроить интернет-эквайринг.', 503)
    endpoint = GATEWAY if os.getenv('SBER_PAYMENTS_TEST', '1') == '0' else 'https://ecomtest.sberbank.ru/ecomm/gw/partner/api/v1/'
    body = dict(payload, userName=os.environ['SBER_MERCHANT_LOGIN'], password=os.environ['SBER_MERCHANT_PASSWORD'])
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as client,
            client.post(endpoint + method + '.do', json=body, allow_redirects=False) as response,
        ):
            if response.status != 200:
                raise AccessError('Платёжный шлюз временно недоступен. Не создавайте повторный счёт.', 503)
            result = await response.json()
        if not isinstance(result, dict):
            raise TypeError
        return result
    except AccessError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
        raise AccessError('Ответ банка не получен. Счёт сохранён; проверьте его статус перед повтором.', 503) from None


async def reconcile(session, bill):
    """Idempotent accounting in the same transaction as the bank receipt."""
    if bill.test_mode != (os.getenv('SBER_PAYMENTS_TEST', '1') != '0'):
        raise AccessError('Счёт относится к другой среде банка. Нужна проверка владельца.', 409)
    payload = {'orderId': bill.bank_order_id} if bill.bank_order_id else {'orderNumber': bill.order_number}
    data = await bank_call('getOrderStatusExtended', payload)
    bill.checked_at = utc_now()
    if str(data.get('errorCode', '0')) != '0':
        return
    if (data.get('orderNumber') != bill.order_number or str(data.get('currency')) != '643'
            or type(data.get('amount')) is not int or data['amount'] != int(money(bill.amount) * 100)):
        raise AccessError('Ответ банка не совпал с номером, валютой или суммой счёта. Нужна проверка владельца.', 409)
    state = data.get('orderStatus')
    if type(state) is not int:
        return
    if state == 2:
        if bill.test_mode:
            bill.status = 'TEST_PAID'
            return
        info = data.get('paymentAmountInfo') or {}
        refunded = info.get('refundedAmount', 0)
        if type(refunded) is not int or not 0 <= refunded <= data['amount']:
            raise AccessError('Банк вернул неоднозначные данные возврата. Нужна проверка владельца.', 409)
        net = money((data['amount'] - refunded) / 100)
        if not bill.receipt_id and net > 0:
            receipt = Receipt(booking_id=bill.booking_id, uploaded_by_id=bill.created_by_id,
                purpose='DEPOSIT' if json.loads(bill.pricing_snapshot).get('kind') == 'DEPOSIT' else 'PAYMENT', file_id='', file_unique_id='sber-order:' + bill.order_number,
                expected_amount=bill.amount, verified_amount=net, status='APPROVED',
                analysis=json.dumps({'status': 'bank_confirmed', 'provider': 'sber', 'orderNumber': bill.order_number}),
                reviewed_at=utc_now())
            session.add(receipt)
            await session.flush()
            bill.receipt_id = receipt.id
        elif bill.receipt_id:
            receipt = await session.get(Receipt, bill.receipt_id, with_for_update=True)
            receipt.status, receipt.verified_amount = ('APPROVED', net) if net > 0 else ('REJECTED', None)
        bill.status = 'PAID' if not refunded else 'PARTIAL_REFUND' if net else 'REFUNDED'
    elif state in {3, 4}:
        bill.status = 'REFUNDED' if state == 4 else 'CANCELLED'
        if bill.receipt_id:
            receipt = await session.get(Receipt, bill.receipt_id, with_for_update=True)
            receipt.status, receipt.verified_amount = 'REJECTED', None
    elif state == 6:
        bill.status = 'DECLINED'
    await session.flush()
    await refresh_payment_statuses(session, bill.booking_id)


def public_bill(bill):
    return {'id': bill.id, 'bookingId': bill.booking_id, 'amount': str(money(bill.amount)),
            'status': bill.status, 'url': bill.payment_url,
            'testMode': bill.test_mode,
            'qrPath': f'/bank-payments/{bill.id}/qr' if bill.payment_url else None}


class BankPayments:
    def __init__(self, api):
        self.api = api

    async def access(self, session, a, booking):
        actor = await session.get(User, a['id'])
        roles = await roles_of(session, actor) if actor and actor.active else set()
        if not await can_access_booking(session, a['id'], roles, booking):
            raise AccessError('Нет доступа к этой съёмке.', 403)
        return actor

    async def create(self, request):
        body, a = await self.api.body(request), request['miniapp_actor']
        if not configured():
            raise AccessError('СберБизнес ещё не подключён. Нужны настройки интернет-эквайринга владельца.', 503)
        return_url = os.getenv('SBER_RETURN_URL', '')
        if not return_url.startswith('https://'):
            raise AccessError('Владелец должен настроить адрес возврата СберБизнес.', 503)
        optional = {'discountId', 'albumId', 'albumQuantity'}
        if (set(body) - optional != {'bookingId', 'total', 'sold', 'tariffId', 'amount', 'requestKey', 'purpose'}
                or type(body['bookingId']) is not int or type(body['total']) is not int or type(body['sold']) is not int
                or body['purpose'] not in {'PURCHASE', 'DEPOSIT', 'BALANCE'}
                or (body['purpose'] == 'PURCHASE' and not 0 < body['sold'] <= body['total'] <= 10000)
                or not isinstance(body['requestKey'], str) or not 16 <= len(body['requestKey']) <= 80):
            raise AccessError('Выберите съёмку, тариф и количество кадров.', 400)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            booking = await session.get(Booking, body['bookingId'], with_for_update=True)
            actor = await self.access(session, a, booking)
            if booking.status in {'CANCELLED', 'REJECTED'}:
                raise AccessError('Отменённую съёмку оплачивать нельзя.', 409)
            bill = await session.scalar(select(BankPayment).where(BankPayment.request_key == body['requestKey']))
            if bill:
                if bill.created_by_id != a['id'] or bill.payload_hash != digest:
                    raise AccessError('Ключ счёта уже использован для других данных.', 409)
                return web.json_response(public_bill(bill))
            pending = await session.scalar(select(BankPayment).where(BankPayment.booking_id == booking.id,
                BankPayment.status.in_(['CREATING', 'PENDING'])).limit(1))
            if pending:
                raise AccessError(f'По съёмке уже есть счёт №{pending.id}. Проверьте его перед созданием нового.', 409)
            try:
                from .services.sale_workflow import validate_remaining_photos
                previous, paid, _ = await payment_totals(session, booking.id)
                if body['purpose'] == 'PURCHASE':
                    await validate_remaining_photos(session, booking, body['total'], body['sold'])
                    from .services.sale_workflow import active_draft
                    draft = await active_draft(session, booking.id)
                    frozen = draft.pricing_snapshot if draft else None
                    if frozen and (draft.sold_photos != body['sold'] or draft.declared_photo_count != body['total']):
                        raise ValueError('Количество кадров не совпадает с сохранённым расчётом продажи.')
                    quote = await sale_quote(session, booking, body['sold'], body['total'], body['tariffId'],
                        saved=frozen,
                        discount_id=body.get('discountId'), album_id=body.get('albumId'),
                        album_quantity=body.get('albumQuantity'))
                    due = max(money(previous) + money(quote['amount']) - money(paid), money(0))
                elif body['purpose'] == 'DEPOSIT':
                    deposits = await session.scalar(select(func.coalesce(func.sum(Receipt.verified_amount), 0)).where(
                        Receipt.booking_id == booking.id, Receipt.purpose == 'DEPOSIT', Receipt.status == 'APPROVED'))
                    due = max(money(booking.deposit) - money(deposits), money(0))
                    quote = {'kind': 'DEPOSIT', 'amount': str(due)}
                else:
                    due = max(money(previous) - money(paid), money(0))
                    quote = {'kind': 'BALANCE', 'amount': str(due)}
                amount = due if body['amount'] is None else money(body['amount'])
                if not 0 < amount <= due:
                    raise ValueError('Сумма оплаты должна быть положительной и не превышать остаток.')
            except (ValueError, ArithmeticError, TypeError) as exc:
                raise AccessError(str(exc), 400) from exc
            bill = BankPayment(booking_id=booking.id, created_by_id=actor.id, request_key=body['requestKey'],
                payload_hash=digest, order_number='pb-' + uuid4().hex, amount=amount,
                test_mode=os.getenv('SBER_PAYMENTS_TEST', '1') != '0',
                pricing_snapshot=json.dumps(quote, ensure_ascii=False))
            session.add(bill)
            await session.flush()
            await audit(session, actor, 'bank_bill_created', 'bank_payment', bill.id, f'booking={booking.id};amount={amount}')
            # Keep an uncertain registration durable; never repeat with a new order number.
            await session.commit()
            hotel = await session.get(Hotel, booking.hotel_id)
            response = await bank_call('register', {'orderNumber': bill.order_number,
                'amount': int(amount * 100), 'currency': '643', 'returnUrl': return_url,
                'description': f'Photo Boss · {hotel.name} · съёмка {booking.id} · {body["purpose"]} · {body["sold"]} фото',
                'sessionTimeoutSecs': 1200})
            if str(response.get('errorCode', '0')) != '0' or not response.get('orderId') or not bank_url(response.get('formUrl')):
                raise AccessError('Банк не подтвердил создание платёжной ссылки. Счёт сохранён для сверки.', 503)
            bill.bank_order_id, bill.payment_url, bill.status = response['orderId'], response['formUrl'], 'PENDING'
            await session.commit()
        return web.json_response(public_bill(bill))

    async def status(self, request):
        a = request['miniapp_actor']
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            preview = await session.get(BankPayment, int(request.match_info['id']))
            if not preview:
                raise AccessError('Счёт не найден.', 404)
            booking = await session.get(Booking, preview.booking_id, with_for_update=True)
            await self.access(session, a, booking)
            bill = await session.get(BankPayment, preview.id, with_for_update=True, populate_existing=True)
            await reconcile(session, bill)
            await session.commit()
            return web.json_response(public_bill(bill))

    async def qr(self, request):
        async with AsyncSession(self.api.engine) as session:
            bill = await session.get(BankPayment, int(request.match_info['id']))
            if not bill:
                raise AccessError('Счёт не найден.', 404)
            await self.access(session, request['miniapp_actor'], await session.get(Booking, bill.booking_id))
            if bill.status != 'PENDING' or not bank_url(bill.payment_url):
                raise AccessError('Для этого счёта нет активного QR.', 409)
            image = qrcode.make(bill.payment_url)
            data = io.BytesIO()
            image.save(data, format='PNG')
            return web.Response(body=data.getvalue(), content_type='image/png', headers={'Cache-Control': 'no-store'})


async def payment_loop(engine, interval=30):
    while True:
        if configured():
            try:
                from .operations_guard import operation_lease
                async with operation_lease(engine):
                    async with AsyncSession(engine) as session:
                        ids = list(await session.scalars(select(BankPayment.id).where(
                            BankPayment.status.in_(['CREATING', 'PENDING'])).order_by(BankPayment.id).limit(50)))
                    for bid in ids:
                        async with AsyncSession(engine) as session:
                            preview = await session.get(BankPayment, bid)
                            await session.get(Booking, preview.booking_id, with_for_update=True)
                            bill = await session.get(BankPayment, bid, with_for_update=True, populate_existing=True)
                            await reconcile(session, bill)
                            await session.commit()
            except (AccessError, SQLAlchemyError, OSError) as exc:
                log.warning('Bank reconciliation deferred: %s', type(exc).__name__)
        await asyncio.sleep(interval)


def install_bank_payments(app, api):
    service = BankPayments(api)
    app.router.add_post('/api/miniapp/bank-payments', service.create)
    app.router.add_post('/api/miniapp/bank-payments/{id:\\d+}/status', service.status)
    app.router.add_get('/api/miniapp/bank-payments/{id:\\d+}/qr', service.qr)
