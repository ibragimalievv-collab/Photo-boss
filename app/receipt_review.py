"""Review existing receipt evidence without confusing it with bank settlement.

Automatic mode displays the independent document admission result. Legacy mode
retains explicit owner review. Both use the same receipt/payment ledger.
"""
import json
import re
from types import SimpleNamespace

from aiohttp import web
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .bank_receipt_basis import RECONCILIATION_REQUIRED
from .miniapp_security import AccessError
from .models import BankPayment, Booking, Client, Hotel, Receipt, utc_now
from .operational_start import OperationalStartError, require_booking_date
from .services.core import audit
from .services.receipts import (
    duplicate_receipt,
    money,
    payment_totals,
    refresh_payment_statuses,
    warnings_for,
)


def owner(request):
    actor = request['miniapp_actor']
    if 'OWNER' not in actor['roles']:
        raise AccessError('Поступление денег подтверждает только владелец.', 403)
    return actor


def receipt_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', value):
        raise AccessError('Некорректный номер чека.', 400)
    value = int(value)
    if value >= 2**31:
        raise AccessError('Некорректный номер чека.', 400)
    return value


def decision_body(body):
    if not isinstance(body, dict) or body.get('expectedStatus') != 'PENDING':
        raise AccessError('Обновите чек перед проверкой.', 400)
    action = body.get('action')
    if action == 'reject' and set(body) == {'action', 'expectedStatus'}:
        return action, None
    if (action != 'approve' or set(body) != {'action', 'expectedStatus', 'verifiedAmount', 'bankStatementChecked'}
            or body['bankStatementChecked'] is not True):
        raise AccessError('Сначала сверьте поступление, получателя и сумму в банковской выписке.', 400)
    raw = body['verifiedAmount']
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9]{1,8}(?:[.,][0-9]{1,2})?', raw):
        raise AccessError('Введите фактически поступившую сумму в рублях, не более двух знаков после запятой.', 400)
    amount = money(raw.replace(',', '.'))
    if not 0 < amount <= 10_000_000:
        raise AccessError('Введите сумму от 0,01 до 10 000 000 рублей.', 400)
    return action, amount


class ReceiptReview:
    def __init__(self, api):
        self.api = api

    async def payload(self, session, row):
        booking = await session.get(Booking, row.booking_id)
        hotel = await session.get(Hotel, booking.hotel_id)
        client = await session.get(Client, booking.client_id)
        try:
            analysis = json.loads(row.analysis or '{}')
        except (ValueError, TypeError):
            analysis = {}
        analysis = analysis if isinstance(analysis, dict) else {}
        extracted = analysis.get('status') == 'extracted' and isinstance(analysis.get('fields'), dict)
        warnings = (warnings_for(analysis['fields'], row.expected_amount, row.created_at.date())
                    if extracted else ['Распознавание не завершено. Сверьте фото и банковскую выписку вручную.'])
        duplicate = bool(await duplicate_receipt(session, row, approved_only=True))
        if duplicate:
            warnings.append('Похожий чек или номер операции уже подтверждён.')
        from .services.automatic_receipts import enabled, public_state
        automatic = public_state(row) or ({'state': 'queued', 'messages': []} if enabled() else None)
        if automatic:
            warnings = automatic.get('messages', [])
            if duplicate:
                warnings = [*warnings, 'Похожий платёж уже учтён. Повторного зачёта не будет.']
        from .workflow import receipt_state
        return {'id': row.id, 'bookingId': row.booking_id, 'client': client.name if client else '',
            'automatic': automatic, 'bankConfirmed': analysis.get('status') == 'bank_confirmed' or bool(analysis.get('bankSettlement')),
            'images': receipt_state(row)['images'],
            'hotel': hotel.name if hotel else '', 'purpose': row.purpose, 'status': row.status,
            'expectedAmount': str(money(row.expected_amount)),
            'verifiedAmount': str(money(row.verified_amount)) if row.verified_amount is not None else None,
            'imagePath': f'/receipts/{row.id}/image' if row.file_id else None,
            'analysisStatus': 'extracted' if extracted else 'unavailable', 'warnings': warnings,
            'duplicateApproved': duplicate, 'reviewedById': row.reviewed_by_id,
            'reviewedAt': row.reviewed_at.isoformat() if row.reviewed_at else None}

    async def result(self, session, row, *, repeated=False):
        total, paid, remaining = await payment_totals(session, row.booking_id)
        return {'receipt': await self.payload(session, row), 'paymentTotals': {
            'totalAmount': str(total), 'paidAmount': str(paid), 'remainingAmount': str(remaining)},
            'repeated': repeated}

    async def listing(self, request):
        owner(request)
        cursor = request.query.get('cursor', '0')
        if cursor != '0':
            cursor = receipt_id(cursor)
        async with AsyncSession(self.api.engine) as session:
            rows = list(await session.scalars(select(Receipt).where(
                Receipt.status == 'PENDING', Receipt.id > int(cursor)).order_by(Receipt.id).limit(21)))
            items = [await self.payload(session, row) for row in rows[:20]]
        return web.json_response({'items': items, 'nextCursor': rows[19].id if len(rows) > 20 else None})

    async def detail(self, request):
        owner(request)
        rid = receipt_id(request.match_info['id'])
        async with AsyncSession(self.api.engine) as session:
            row = await session.get(Receipt, rid)
            if row is None:
                raise AccessError('Чек не найден.', 404)
            result = await self.result(session, row)
        return web.json_response(result)

    async def decide(self, request):
        actor = owner(request)  # Never parse an unauthorized request body.
        from .services.automatic_receipts import enabled
        if enabled():
            raise AccessError('Чеки проверяются автоматически. Решение владельца для входящих чеков не требуется.', 409)
        rid = receipt_id(request.match_info['id'])
        action, amount = decision_body(await self.api.body(request))
        try:
            async with AsyncSession(self.api.engine, expire_on_commit=False) as session, session.begin():
                bid = await session.scalar(select(Receipt.booking_id).where(Receipt.id == rid))
                if bid is None:
                    raise AccessError('Чек не найден.', 404)
                # Match the bank/sale lock order: booking first, receipt second.
                booking = await session.scalar(select(Booking).where(Booking.id == bid).with_for_update())
                row = await session.scalar(select(Receipt).where(Receipt.id == rid).with_for_update())
                if booking is None or row is None or row.booking_id != bid:
                    raise AccessError('Чек не найден.', 404)
                target = 'APPROVED' if action == 'approve' else 'REJECTED'
                if row.status != 'PENDING':
                    if row.status == target and (action == 'reject' or (
                            row.verified_amount is not None and money(row.verified_amount) == amount)):
                        return web.json_response(await self.result(session, row, repeated=True))
                    raise AccessError('Чек уже обработан. Обновите его состояние.', 409)
                try:
                    require_booking_date(booking.shoot_date)
                except OperationalStartError as error:
                    raise AccessError(str(error), 409) from None
                if action == 'approve':
                    active_bill = await session.scalar(select(BankPayment.id).where(
                        BankPayment.booking_id == bid,
                        BankPayment.status.in_(['CREATING', 'PENDING', RECONCILIATION_REQUIRED])).limit(1))
                    if active_bill:
                        raise AccessError('Банковский счёт ещё проверяется. Дождитесь его окончательного состояния.', 409)
                    settled_bill = await session.scalar(select(BankPayment.id).where(
                        BankPayment.booking_id == bid,
                        or_(BankPayment.receipt_id.is_not(None), BankPayment.status.in_(
                            ['PAID', 'PARTIAL_REFUND', 'REFUNDED']))).limit(1))
                    if settled_bill:
                        # A queued photo may depict the SAME transfer already
                        # recorded by the bank without an image hash. Never
                        # guess a link from its amount/time or credit it twice.
                        raise AccessError('По записи уже есть банковское поступление или возврат. '
                                          'Этот чек нельзя учитывать отдельно без сверки связи с банковским платежом.', 409)
                    if not row.file_id or not re.fullmatch(r'[a-fA-F0-9]{64}', row.image_sha256 or ''):
                        raise AccessError('Фото чека ещё не сохранено полностью. Загрузите его повторно.', 409)
                    if await duplicate_receipt(session, row, approved_only=True):
                        raise AccessError('Этот платёж уже учтён. Повторное подтверждение запрещено.', 409)
                    row.verified_amount = amount
                    row.approved_image_hash = row.image_sha256.lower()
                    row.approved_operation_key = row.operation_key
                row.status, row.reviewed_by_id, row.reviewed_at = target, actor['id'], utc_now()
                await session.flush()
                integration = getattr(self.api, 'booking_refunds', None)
                if integration is not None and action == 'approve':
                    await integration.manual_approved(session, booking, row)
                await refresh_payment_statuses(session, bid)
                await audit(session, SimpleNamespace(id=actor['id']), 'receipt_' + target.lower(),
                            'receipt', rid, 'manual_bank_statement;amount=' + str(amount))
                await session.flush()
                result = await self.result(session, row)
        except IntegrityError:
            raise AccessError('Этот платёж уже подтверждён ранее. Обновите чек.', 409) from None
        return web.json_response(result)


def install_receipt_review(app, api):
    service = ReceiptReview(api)
    app['receipt_review'] = service
    root = '/api/miniapp/receipts/review'
    app.router.add_get(root, service.listing)
    app.router.add_get(root + '/{id}', service.detail)
    app.router.add_post(root + '/{id}/decision', service.decide)
