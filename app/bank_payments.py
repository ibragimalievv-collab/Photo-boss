"""Sber payments with immutable per-bill providers and verified bank accounting.

Existing hosted-page acquiring bills retain their original gateway. QR.API is a
separate, explicitly enabled provider; neither OAuth nor sandbox money is cash.
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import time
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp
import qrcode
from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import BankPayment, Booking, Hotel, Receipt, Sale, SaleDraft, User, utc_now
from .sber_qr_certificates import SberQRCertificateError, build_sber_qr_ssl_context
from .sber_qr_client import (
    QRClient,
    SberQRClientError,
    is_payment_url,
    qr_client_issues,
)
from .sber_qr_config import SberQRConfig
from .services.booking_access import can_access_booking
from .services.core import audit, roles_of
from .services.pricing import sale_quote
from .services.receipts import money, payment_totals, refresh_payment_statuses

log = logging.getLogger(__name__)
GATEWAY = 'https://ecommerce.sberbank.ru/ecomm/gw/partner/api/v1/'
ECOM_PROVIDER = 'sber_ecom'
QR_PROVIDER = 'sber_qr'


def configured():
    """Legacy acquiring gate; QR.API must never substitute its credentials."""
    return os.getenv('SBER_PAYMENTS_ENABLED') == '1' and bool(os.getenv('SBER_MERCHANT_LOGIN') and os.getenv('SBER_MERCHANT_PASSWORD'))


def _qr_requested():
    # A malformed explicit flag must fail closed, rather than choose acquiring.
    return os.getenv('SBER_QR_ENABLED', '0') != '0'


def _ecom_test_mode():
    return os.getenv('SBER_PAYMENTS_TEST', '1') != '0'


def _qr_configuration(*, for_creation):
    try:
        config = SberQRConfig.from_env()
        if (for_creation and not config.enabled) or qr_client_issues(config, for_order=True):
            raise ValueError
        return config
    except (ValueError, OSError):
        raise AccessError('QR Сбера ещё не подключён. Владелец должен проверить настройки и доступ банка.', 503) from None


def payment_settings():
    """Public provider selection without reading certificate contents or bank I/O."""
    if _qr_requested():
        try:
            config = _qr_configuration(for_creation=True)
            complete, test_mode = True, config.environment == 'sandbox'
        except AccessError:
            complete, test_mode = False, os.getenv('SBER_QR_ENV') != 'production'
        return {'bankConfigured': complete, 'bankProvider': QR_PROVIDER, 'bankTestMode': test_mode}
    available = configured()
    return {'bankConfigured': available, 'bankProvider': ECOM_PROVIDER if available else None,
            'bankTestMode': _ecom_test_mode()}


def _bill_metadata(bill):
    try:
        snapshot = json.loads(bill.pricing_snapshot)
        if not isinstance(snapshot, dict):
            raise TypeError
        metadata = snapshot.get('_bank')
        if metadata is None:
            return {'provider': ECOM_PROVIDER,
                    'environment': 'sandbox' if bill.test_mode else 'production'}
        if (not isinstance(metadata, dict) or metadata.get('provider') not in {ECOM_PROVIDER, QR_PROVIDER}
                or metadata.get('environment') not in {'sandbox', 'production'}
                or (metadata['environment'] == 'sandbox') != bill.test_mode):
            raise ValueError
        return metadata
    except (ValueError, TypeError, KeyError):
        raise AccessError('Не удалось определить банковскую среду счёта. Нужна проверка владельца.', 409) from None


def _qr_binding(config):
    # Bind an existing bill to its organization/application, not rotating secrets.
    return hashlib.sha256(json.dumps([config.environment, config.client_id,
        config.member_id, config.id_qr, config.tid], separators=(',', ':')).encode()).hexdigest()


def _bill_payment_url(bill):
    provider = _bill_metadata(bill)['provider']
    return is_payment_url(bill.payment_url) if provider == QR_PROVIDER else bank_url(bill.payment_url)


def bank_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and parsed.hostname in {
                    'ecommerce.sberbank.ru', 'ecomtest.sberbank.ru', 'ecomift.sberbank.ru',
                    'securepayments.sberbank.ru', 'sbergate.com', 'payecom.ru'})
    except ValueError:
        # Invalid ports/brackets in a provider response must not turn an
        # uncertain invoice into an unhandled server error.
        return False


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


async def _approve_verified_payment(session, bill, refunded, *, provider):
    """Existing receipt/deposit ledger, in the caller's locked transaction."""
    if bill.test_mode:
        bill.status = 'TEST_PAID'
        return
    amount_kopecks = int(money(bill.amount) * 100)
    if type(refunded) is not int or not 0 <= refunded <= amount_kopecks:
        raise AccessError('Банк вернул неоднозначные данные возврата. Нужна проверка владельца.', 409)
    net = money((amount_kopecks - refunded) / 100)
    if not bill.receipt_id and net > 0:
        receipt = Receipt(booking_id=bill.booking_id, uploaded_by_id=bill.created_by_id,
            purpose='DEPOSIT' if json.loads(bill.pricing_snapshot).get('kind') == 'DEPOSIT' else 'PAYMENT',
            file_id='', file_unique_id=('sber-qr-order:' if provider == QR_PROVIDER else 'sber-order:') + bill.order_number,
            expected_amount=bill.amount, verified_amount=net, status='APPROVED',
            analysis=json.dumps({'status': 'bank_confirmed',
                'provider': QR_PROVIDER if provider == QR_PROVIDER else 'sber', 'orderNumber': bill.order_number}),
            reviewed_at=utc_now())
        session.add(receipt)
        await session.flush()
        bill.receipt_id = receipt.id
    elif bill.receipt_id:
        receipt = await session.get(Receipt, bill.receipt_id, with_for_update=True)
        receipt.status, receipt.verified_amount = ('APPROVED', net) if net > 0 else ('REJECTED', None)
    bill.status = 'PAID' if not refunded else 'PARTIAL_REFUND' if net else 'REFUNDED'


async def _reconcile_qr(session, bill, metadata):
    config = _qr_configuration(for_creation=False)
    if (metadata['environment'] != config.environment
            or metadata.get('binding') != _qr_binding(config)):
        raise AccessError('Счёт относится к другому приложению или среде банка. Нужна проверка владельца.', 409)
    amount = int(money(bill.amount) * 100)
    try:
        data = await QRClient.from_config(config).get_status(bill.bank_order_id, bill.order_number, amount)
    except SberQRClientError:
        raise AccessError('Подтверждение оплаты по QR от банка не получено. Счёт сохранён для повторной сверки.', 503) from None
    bill.checked_at = utc_now()
    if (not isinstance(data, dict) or data.get('order_number') != bill.order_number
            or data.get('currency') != '643' or type(data.get('amount_kopecks')) is not int
            or data['amount_kopecks'] != amount or type(data.get('test_mode')) is not bool
            or data['test_mode'] != bill.test_mode
            or not isinstance(data.get('bank_order_id'), str) or not 1 <= len(data['bank_order_id']) <= 100
            or (bill.bank_order_id and data['bank_order_id'] != bill.bank_order_id)):
        raise AccessError('Ответ банка не совпал с номером, валютой или суммой счёта. Нужна проверка владельца.', 409)
    state = data.get('status')
    if not isinstance(state, str) or state not in {
            'PENDING', 'PAID', 'TEST_PAID', 'PARTIAL_REFUND', 'REFUNDED', 'CANCELLED', 'DECLINED'}:
        raise AccessError('Банк не подтвердил однозначный статус оплаты. Нужна проверка владельца.', 409)
    if state in {'PAID', 'TEST_PAID', 'PARTIAL_REFUND', 'REFUNDED'}:
        paid, refunded = data.get('paid_kopecks'), data.get('refunded_kopecks')
        if (data.get('settlement_verified') is not True or type(paid) is not int or paid != amount
                or type(refunded) is not int or not 0 <= refunded <= paid
                or (state == 'PAID' and refunded != 0)
                or (state == 'TEST_PAID' and not bill.test_mode)
                or (state == 'PARTIAL_REFUND' and not 0 < refunded < paid)
                or (state == 'REFUNDED' and refunded != paid)):
            raise AccessError('Банк не подтвердил сумму фактической оплаты и возврата. Нужна проверка владельца.', 409)
        if bill.test_mode:
            bill.status = 'REFUNDED' if refunded == paid else 'TEST_PAID'
        else:
            await _approve_verified_payment(session, bill, refunded, provider=QR_PROVIDER)
    else:
        if (type(data.get('paid_kopecks')) is not int or data['paid_kopecks'] != 0
                or type(data.get('refunded_kopecks')) is not int or data['refunded_kopecks'] != 0):
            raise AccessError('Статус банка не совпал с денежными операциями. Нужна проверка владельца.', 409)
        if bill.receipt_id:
            # A previously verified payment cannot be erased by an unpaid state.
            raise AccessError('Банк вернул противоречивый статус ранее оплаченного счёта.', 409)
        bill.status = state
    bill.bank_order_id = data['bank_order_id']
    await session.flush()
    await refresh_payment_statuses(session, bill.booking_id)


async def reconcile(session, bill):
    """Idempotent accounting using the provider recorded when the bill was made."""
    metadata = _bill_metadata(bill)
    if metadata['provider'] == QR_PROVIDER:
        return await _reconcile_qr(session, bill, metadata)
    if bill.test_mode != _ecom_test_mode():
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
        await _approve_verified_payment(session, bill, refunded, provider=ECOM_PROVIDER)
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
            'testMode': bill.test_mode, 'provider': _bill_metadata(bill)['provider'],
            'qrPath': f'/bank-payments/{bill.id}/qr' if bill.payment_url else None}


def _connection_readiness():
    """Only allowlisted metadata; never parse a certificate or contact the bank."""
    result = {'provider': QR_PROVIDER, 'enabled': False, 'environment': 'sandbox',
        'authenticationConfigurationComplete': False, 'orderConfigurationComplete': False,
        'orderCreationEnabled': False, 'issues': [],
        'certificate': {'checked': False, 'valid': None},
        'oauth': {'checked': False, 'authenticated': False},
        'ordersCreated': False, 'paymentsVerified': False}
    try:
        config = SberQRConfig.from_env()
    except (ValueError, OSError):
        result['issues'] = ['qr_configuration_invalid']
        return None, result
    auth_issues = qr_client_issues(config, for_order=False)
    order_issues = qr_client_issues(config, for_order=True)
    result.update(enabled=config.enabled, environment=config.environment,
        authenticationConfigurationComplete=not auth_issues,
        orderConfigurationComplete=not order_issues,
        orderCreationEnabled=config.enabled and not order_issues,
        issues=list(order_issues))
    return config, result


class BankPayments:
    def __init__(self, api):
        self.api = api

    async def access(self, session, a, booking):
        actor = await session.get(User, a['id'])
        roles = await roles_of(session, actor) if actor and actor.active else set()
        if not await can_access_booking(session, a['id'], roles, booking):
            raise AccessError('Нет доступа к этой съёмке.', 403)
        return actor

    async def _owner(self, request):
        async with AsyncSession(self.api.engine) as session:
            actor = await session.get(User, request['miniapp_actor']['id'])
            roles = await roles_of(session, actor) if actor and actor.active else set()
            if 'OWNER' not in roles:
                raise AccessError('Проверка подключения банка доступна только владельцу.', 403)

    async def connection(self, request):
        await self._owner(request)
        _, result = _connection_readiness()
        return web.json_response(result, headers={'Cache-Control': 'no-store'})

    async def check_connection(self, request):
        await self._owner(request)
        if await self.api.body(request) != {}:
            raise AccessError('Проверка использует только настройки сервера. Лишние параметры запрещены.', 400)
        config, result = _connection_readiness()
        result.update(ok=False, scope='certificate_and_oauth_only', stage='configuration')
        if config is None or not result['authenticationConfigurationComplete']:
            result['error'] = 'Для проверки банка не хватает подтверждённых настроек подключения.'
            return web.json_response(result, status=503, headers={'Cache-Control': 'no-store'})
        now = time.monotonic()
        if now - getattr(self, '_connection_probe_at', -1000) < 60:
            raise AccessError('Проверка банка уже выполнялась. Повтор доступен через минуту.', 429)
        self._connection_probe_at = now
        result['stage'] = 'certificate'
        result['certificate']['checked'] = True
        try:
            context = build_sber_qr_ssl_context(config)
        except SberQRCertificateError:
            result['certificate']['valid'] = False
            result['error'] = 'Клиентский сертификат не прошёл проверку. Проверьте файл и пароль на сервере.'
            return web.json_response(result, status=503, headers={'Cache-Control': 'no-store'})
        result['certificate']['valid'] = True
        result['stage'] = 'oauth'
        result['oauth']['checked'] = True
        try:
            checked = await QRClient.from_config(config, ssl_context=context).check_connection()
        except SberQRClientError:
            result['error'] = 'Банк не подтвердил доступ приложения. Оплата не включена.'
            return web.json_response(result, status=503, headers={'Cache-Control': 'no-store'})
        if (not isinstance(checked, dict) or checked.get('authenticated') is not True
                or checked.get('environment') != config.environment):
            result['error'] = 'Банк не подтвердил доступ приложения. Оплата не включена.'
            return web.json_response(result, status=503, headers={'Cache-Control': 'no-store'})
        result['oauth']['authenticated'] = True
        result.update(ok=True, stage='complete')
        return web.json_response(result, headers={'Cache-Control': 'no-store'})

    async def create(self, request):
        body, a = await self.api.body(request), request['miniapp_actor']
        optional = {'discountId', 'albumId', 'albumQuantity', 'sourceDraftId', 'allowZeroDue'}
        if (set(body) - optional != {'bookingId', 'total', 'sold', 'tariffId', 'amount', 'requestKey', 'purpose'}
                or type(body['bookingId']) is not int or type(body['total']) is not int or type(body['sold']) is not int
                or not isinstance(body['purpose'], str) or body['purpose'] not in {'PURCHASE', 'DEPOSIT', 'BALANCE'}
                or (body.get('sourceDraftId') is not None and
                    (type(body['sourceDraftId']) is not int or body['sourceDraftId'] <= 0))
                or ('allowZeroDue' in body and type(body['allowZeroDue']) is not bool)
                or (body.get('allowZeroDue') and
                    (body['purpose'] != 'PURCHASE' or not body.get('sourceDraftId')))
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
                    from .services.sale_workflow import active_draft, can_sell
                    source_id = body.get('sourceDraftId')
                    draft = await session.get(SaleDraft, source_id, with_for_update=True,
                        populate_existing=True) if source_id else await active_draft(session, booking.id)
                    if source_id and (not draft or draft.booking_id != booking.id or draft.status == 'CANCELLED'):
                        raise ValueError('Черновик счёта недоступен в этой съёмке. Обновите продажу.')
                    if draft and draft.created_by_id != actor.id:
                        raise ValueError('Эту продажу уже оформляет другой сотрудник.')
                    if draft and not await can_sell(actor, await roles_of(session, actor), booking, session=session):
                        raise ValueError('Эта запись ещё не готова к продаже или недоступна.')
                    frozen = draft.pricing_snapshot if draft else None
                    recorded = json.loads(frozen) if frozen else {}
                    if frozen and (recorded.get('sold', draft.sold_photos) != body['sold']
                            or recorded.get('total', draft.declared_photo_count) != body['total']):
                        raise ValueError('Количество кадров не совпадает с сохранённым расчётом продажи.')
                    if recorded.get('_bankQuote') and body.get('discountId') not in (None, recorded.get('discountId')):
                        raise ValueError('Скидка уже закреплена в банковском счёте.')
                    if draft and draft.status == 'COMPLETED':
                        if not await session.scalar(select(Sale.id).where(Sale.source_draft_id == draft.id,
                                                                         Sale.booking_id == booking.id)):
                            raise ValueError('Не найдена оформленная продажа для этого счёта.')
                        # The last confirmed original can finish the sale while
                        # the bank request is in flight. Invoice its balance,
                        # never add the same displayed purchase a second time.
                        due = max(money(previous) - money(paid), money(0))
                        quote = {'kind': 'BALANCE', 'amount': str(due), 'sourceDraftId': draft.id}
                    else:
                        if previous > 0 and not draft:
                            raise ValueError('Для оплаты оформленной продажи выберите остаток. Для новой покупки сначала откройте её черновик.')
                        await validate_remaining_photos(session, booking, body['total'], body['sold'])
                        quote = await sale_quote(session, booking, body['sold'], body['total'], body['tariffId'],
                            saved=frozen,
                            discount_id=body.get('discountId'), album_id=body.get('albumId'),
                            album_quantity=body.get('albumQuantity'))
                        if draft:
                            # The quote and invoice commit together before bank
                            # I/O. Counts remain unaccepted until payment, but
                            # tariffs/products cannot drift while QR is open.
                            quote['_bankQuote'] = True
                            draft.pricing_snapshot = json.dumps(quote, ensure_ascii=False)
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
                if body.get('allowZeroDue') and due == 0 and amount == 0:
                    # Explicit proof from the locked ledger, never a client
                    # estimate or a generic validation failure. No bank bill.
                    await session.commit()
                    return web.json_response({'allowZeroDue': True, 'status': 'NO_PAYMENT_REQUIRED',
                        'bookingId': booking.id, 'sourceDraftId': draft.id,
                        'amount': '0.00', 'testMode': False})
                if not 0 < amount <= due:
                    raise ValueError('Сумма оплаты должна быть положительной и не превышать остаток.')
            except (ValueError, ArithmeticError, TypeError) as exc:
                raise AccessError(str(exc), 400) from exc
            qr_config = None
            if _qr_requested():
                qr_config = _qr_configuration(for_creation=True)
                provider, test_mode = QR_PROVIDER, qr_config.environment == 'sandbox'
            else:
                if not configured():
                    raise AccessError('СберБизнес ещё не подключён. Нужны настройки банковского сервиса владельца.', 503)
                provider, test_mode = ECOM_PROVIDER, _ecom_test_mode()
                return_url = os.getenv('SBER_RETURN_URL', '')
                if not return_url.startswith('https://'):
                    raise AccessError('Владелец должен настроить адрес возврата СберБизнес.', 503)
            quote['_bank'] = {'provider': provider, 'environment': 'sandbox' if test_mode else 'production'}
            if qr_config is not None:
                quote['_bank']['binding'] = _qr_binding(qr_config)
            bill = BankPayment(booking_id=booking.id, created_by_id=actor.id, request_key=body['requestKey'],
                payload_hash=digest, order_number='pb-' + uuid4().hex, amount=amount,
                test_mode=test_mode,
                pricing_snapshot=json.dumps(quote, ensure_ascii=False))
            session.add(bill)
            await session.flush()
            await audit(session, actor, 'bank_bill_created', 'bank_payment', bill.id, f'booking={booking.id};amount={amount}')
            hotel = await session.get(Hotel, booking.hotel_id)
            description = f'Photo Boss · {hotel.name} · съёмка {booking.id} · {body["purpose"]} · {body["sold"]} фото'
            # Keep an uncertain registration durable; never repeat with a new order number.
            await session.commit()
            if qr_config is not None:
                try:
                    response = await QRClient.from_config(qr_config).create_order(
                        bill.order_number, int(amount * 100), ' '.join(description.split())[:256])
                except SberQRClientError:
                    raise AccessError('Банк не подтвердил создание QR. Счёт сохранён для сверки; не создавайте повторный.', 503) from None
                if (not isinstance(response, dict) or response.get('status') != 'PENDING'
                        or type(response.get('test_mode')) is not bool or response['test_mode'] != bill.test_mode
                        or not isinstance(response.get('bank_order_id'), str)
                        or not 1 <= len(response['bank_order_id']) <= 100
                        or not is_payment_url(response.get('payment_url'))):
                    raise AccessError('Банк не подтвердил создание QR. Счёт сохранён для сверки.', 503)
                registered_order_id, payment_url = response['bank_order_id'], response['payment_url']
            else:
                response = await bank_call('register', {'orderNumber': bill.order_number,
                    'amount': int(amount * 100), 'currency': '643', 'returnUrl': return_url,
                    'description': description, 'sessionTimeoutSecs': 1200})
                if str(response.get('errorCode', '0')) != '0' or not response.get('orderId') or not bank_url(response.get('formUrl')):
                    raise AccessError('Банк не подтвердил создание платёжной ссылки. Счёт сохранён для сверки.', 503)
                registered_order_id, payment_url = response['orderId'], response['formUrl']
            # Status polling can finish while registration is in flight. Lock
            # in the normal booking→bill order, reload, and never overwrite a
            # confirmed payment/refund with the registration's PENDING state.
            await session.get(Booking, bill.booking_id, with_for_update=True, populate_existing=True)
            bill = await session.get(BankPayment, bill.id, with_for_update=True, populate_existing=True)
            if bill.bank_order_id and bill.bank_order_id != registered_order_id:
                raise AccessError('Ответ регистрации не совпал с ранее подтверждённым счётом банка.', 409)
            bill.bank_order_id = registered_order_id
            if bill.status in {'CREATING', 'PENDING'}:
                bill.payment_url = payment_url
                bill.status = 'PENDING'
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
            if bill.status != 'PENDING' or not _bill_payment_url(bill):
                raise AccessError('Для этого счёта нет активного QR.', 409)
            image = qrcode.make(bill.payment_url)
            data = io.BytesIO()
            image.save(data, format='PNG')
            return web.Response(body=data.getvalue(), content_type='image/png', headers={'Cache-Control': 'no-store'})


async def payment_loop(engine, interval=30):
    while True:
        try:
            qr_polling = not qr_client_issues(SberQRConfig.from_env(), for_order=True)
        except (ValueError, OSError):
            qr_polling = False
        if configured() or qr_polling:
            try:
                from .operations_guard import operation_lease
                async with operation_lease(engine):
                    async with AsyncSession(engine) as session:
                        ids = list(await session.scalars(select(BankPayment.id).where(
                            BankPayment.status.in_(['CREATING', 'PENDING'])).order_by(BankPayment.id).limit(50)))
                    for bid in ids:
                        try:
                            async with AsyncSession(engine) as session:
                                preview = await session.get(BankPayment, bid)
                                if preview is None:
                                    continue
                                await session.get(Booking, preview.booking_id, with_for_update=True)
                                bill = await session.get(BankPayment, bid, with_for_update=True, populate_existing=True)
                                await reconcile(session, bill)
                                await session.commit()
                        except (AccessError, SQLAlchemyError, OSError) as exc:
                            # A bill requiring old credentials must not block the
                            # other provider's bills. Never log credentials or IDs.
                            log.warning('Bank bill reconciliation deferred: %s', type(exc).__name__)
            except (AccessError, SQLAlchemyError, OSError) as exc:
                log.warning('Bank reconciliation deferred: %s', type(exc).__name__)
        await asyncio.sleep(interval)


def install_bank_payments(app, api):
    service = BankPayments(api)
    app.router.add_get('/api/miniapp/bank-payments/connection', service.connection)
    app.router.add_post('/api/miniapp/bank-payments/connection/check', service.check_connection)
    app.router.add_post('/api/miniapp/bank-payments', service.create)
    app.router.add_post('/api/miniapp/bank-payments/{id:\\d+}/status', service.status)
    app.router.add_get('/api/miniapp/bank-payments/{id:\\d+}/qr', service.qr)
