"""Freeze credited ledger rows, not a guessed identity of a bank transfer.

Callers hold the booking lock. A changed ledger needs reconciliation before the
first bank credit; a separately verified bank fact remains durable without cash.
"""
import json

from sqlalchemy import select

from .miniapp_security import AccessError
from .models import Receipt
from .services.core import audit
from .services.receipts import money

RECONCILIATION_REQUIRED = 'RECONCILIATION_REQUIRED'
BASIS_KEY = '_receiptBasis'
REVIEW_KEY = '_bankSettlementReview'
REVIEW_MESSAGE = ('Банк подтвердил денежную операцию. Связь с уже учтёнными поступлениями '
                  'требует сверки владельца. Повторное поступление не добавлено; новый QR создавать нельзя.')


def snapshot(bill):
    try:
        value = json.loads(bill.pricing_snapshot)
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (ValueError, TypeError):
        raise AccessError('Не удалось прочитать расчёт банковского счёта. Нужна сверка владельца.', 409) from None


async def receipt_basis(session, booking_id):
    rows = (await session.execute(select(Receipt.id, Receipt.purpose, Receipt.verified_amount).where(
        Receipt.booking_id == booking_id, Receipt.status == 'APPROVED').order_by(Receipt.id))).all()
    result = []
    for rid, purpose, verified in rows:
        try:
            amount = money(verified)
            if amount <= 0 or purpose not in {'DEPOSIT', 'PAYMENT'}:
                raise ValueError
        except (ArithmeticError, ValueError, TypeError):
            raise AccessError('Учтённое поступление повреждено. Нужна сверка владельца.', 409) from None
        result.append({'id': rid, 'purpose': purpose, 'verifiedAmount': str(amount)})
    return {'version': 1, 'receipts': result}


def valid_basis(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'receipts'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['receipts'], list)):
        return False
    previous = 0
    for row in value['receipts']:
        if (not isinstance(row, dict) or set(row) != {'id', 'purpose', 'verifiedAmount'}
                or type(row['id']) is not int or row['id'] <= previous
                or row['purpose'] not in ('DEPOSIT', 'PAYMENT')
                or not isinstance(row['verifiedAmount'], str)):
            return False
        try:
            amount = money(row['verifiedAmount'])
            if amount <= 0 or str(amount) != row['verifiedAmount']:
                return False
        except (ArithmeticError, ValueError, TypeError):
            return False
        previous = row['id']
    return True


async def first_credit_conflict(session, bill):
    try:
        current = await receipt_basis(session, bill.booking_id)
    except AccessError:
        return 'receipt_ledger_invalid'
    saved = snapshot(bill)
    if BASIS_KEY not in saved:
        # Legacy bills can only be auto-credited against an empty ledger. Never
        # invent a prior basis for an invoice whose creation did not save one.
        return 'legacy_receipt_basis_missing' if current['receipts'] else None
    if not valid_basis(saved[BASIS_KEY]):
        return 'receipt_basis_invalid'
    return 'receipt_basis_changed' if saved[BASIS_KEY] != current else None


def has_settlement_review(bill):
    return REVIEW_KEY in snapshot(bill)


async def record_settlement_review(session, bill, *, provider, refunded, net, reason):
    saved = snapshot(bill)
    previous = saved.get(REVIEW_KEY)
    # Preserve the original conflict reason, including on a later full refund.
    reason = (previous.get('reason', reason) if isinstance(previous, dict) else reason) or 'settlement_review_present'
    fact = {'reason': reason, 'provider': provider, 'orderNumber': bill.order_number,
            'bankOrderId': bill.bank_order_id, 'grossAmount': str(money(bill.amount)),
            'refundedKopecks': refunded, 'netAmount': str(money(net))}
    bill.status = RECONCILIATION_REQUIRED
    if previous != fact:
        saved[REVIEW_KEY] = fact
        bill.pricing_snapshot = json.dumps(saved, ensure_ascii=False)
        await audit(session, None, 'bank_settlement_requires_review', 'bank_payment', bill.id,
                    json.dumps(fact, ensure_ascii=False, sort_keys=True))


def public_settlement_review(bill):
    value = snapshot(bill).get(REVIEW_KEY)
    if not isinstance(value, dict):
        return None
    return {'reason': value['reason'], 'grossAmount': value['grossAmount'],
            'refundedKopecks': value['refundedKopecks'], 'netAmount': value['netAmount']}
