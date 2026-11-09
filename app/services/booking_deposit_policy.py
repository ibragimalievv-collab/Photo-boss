"""Pure cancellation policy; this module never moves or records money.

Callers must load the actor's current booking permissions and lock the booking,
its immutable reserved-slot/notice facts and payment rows first.  These facts are
server-created records, never fields accepted from a cancellation HTTP body.
Restore/reschedule must retain them rather than reinterpret an edited date.
"""
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

POLICY_VERSION = 'booking-deposit-moscow-day-v1'
RESERVATION_DEPOSIT_KOPECKS = 100_000
MOSCOW = ZoneInfo('Europe/Moscow')


class DepositPolicyError(ValueError):
    """Facts are incomplete/inconsistent; do not initiate an automatic refund."""


@dataclass(frozen=True)
class ReservedSlotSnapshot:
    booking_id: int
    revision: str
    shoot_date: date
    shoot_time: time
    captured_at: datetime


@dataclass(frozen=True)
class CancellationNotice:
    booking_id: int
    slot_revision: str
    received_at: datetime
    reason: str
    force_majeure: bool = False


@dataclass(frozen=True)
class OriginalBankPayment:
    """Binding and amounts already verified against the original bank payment.

    OCR operation IDs or a permanent merchant QR do not establish this binding.
    The bank adapter also owns durable intent/idempotency and unknown outcomes.
    """
    bank_payment_id: int
    bank_order_id: str
    booking_id: int
    receipt_id: int
    paid_kopecks: int
    refunded_kopecks: int
    status: str
    test_mode: bool


@dataclass(frozen=True)
class DepositReceipt:
    receipt_id: int
    purpose: str
    status: str
    verified_net_kopecks: int | None
    original: OriginalBankPayment | None = None


@dataclass(frozen=True)
class OriginalPaymentRefund:
    receipt_id: int
    bank_payment_id: int
    bank_order_id: str
    amount_kopecks: int


@dataclass(frozen=True)
class CancellationDepositDecision:
    policy_version: str
    category: str
    notice_moscow_date: date
    reserved_date: date
    reason: str
    disposition: str
    verified_net_kopecks: int
    retained_kopecks: int
    eligible_refund_kopecks: int
    refunds: tuple[OriginalPaymentRefund, ...]

    @property
    def photo_credit_kopecks(self):
        # A cancelled reservation never makes a retained/refunding sum available
        # to a restored booking. Successful shoots use the existing sale ledger.
        return 0


def _id(value):
    return type(value) is int and value > 0


def _amount(value):
    return type(value) is int and 0 <= value <= 99_999_999_999_999


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _revision(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and value == value.strip()


def _validate_facts(slot, notice, checked_at):
    if not isinstance(slot, ReservedSlotSnapshot) or not isinstance(notice, CancellationNotice):
        raise DepositPolicyError('Immutable reservation and server notice facts are required.')
    if (not _id(slot.booking_id) or slot.booking_id != notice.booking_id
            or not _id(notice.booking_id) or not _revision(slot.revision)
            or slot.revision != notice.slot_revision):
        raise DepositPolicyError('Reserved slot and notice do not match; reconciliation is required.')
    if (type(slot.shoot_date) is not date or type(slot.shoot_time) is not time
            or slot.shoot_time.tzinfo is not None):
        raise DepositPolicyError('The reserved Moscow calendar slot is invalid.')
    if not all(_aware(value) for value in (slot.captured_at, notice.received_at, checked_at)):
        raise DepositPolicyError('Server timestamps must include their time zone.')
    if not slot.captured_at <= notice.received_at <= checked_at:
        raise DepositPolicyError('Notice chronology is uncertain; reconciliation is required.')
    if (not isinstance(notice.reason, str) or not 3 <= len(notice.reason.strip()) <= 500
            or type(notice.force_majeure) is not bool):
        raise DepositPolicyError('A cancellation explanation of 3 to 500 characters is required.')


def _confirmed_deposits(receipts):
    confirmed, seen = [], set()
    for receipt in receipts:
        if (not isinstance(receipt, DepositReceipt) or not _id(receipt.receipt_id)
                or receipt.receipt_id in seen):
            raise DepositPolicyError('Receipt facts are invalid or duplicated.')
        seen.add(receipt.receipt_id)
        if (not isinstance(receipt.purpose, str) or not isinstance(receipt.status, str)
                or receipt.purpose not in {'DEPOSIT', 'PAYMENT'}
                or receipt.status not in {'PENDING', 'APPROVED', 'REJECTED'}):
            raise DepositPolicyError('Unknown receipt purpose or status.')
        if receipt.status != 'APPROVED' or receipt.purpose != 'DEPOSIT':
            continue
        if not _amount(receipt.verified_net_kopecks):
            raise DepositPolicyError('An approved deposit needs an exact verified net amount.')
        if receipt.verified_net_kopecks:
            confirmed.append(receipt)
    return confirmed


def _original_refund(receipt, booking_id):
    original = receipt.original
    if original is None:
        return None, 'BLOCKED_ORIGIN_UNAVAILABLE'
    if not isinstance(original, OriginalBankPayment):
        return None, 'BLOCKED_PAYMENT_RECONCILIATION'
    if not isinstance(original.bank_order_id, str) or not original.bank_order_id.strip():
        return None, 'BLOCKED_ORIGIN_UNAVAILABLE'
    if (not _id(original.bank_payment_id) or not _id(original.booking_id)
            or not _id(original.receipt_id) or len(original.bank_order_id) > 100
            or original.bank_order_id != original.bank_order_id.strip()
            or original.booking_id != booking_id or original.receipt_id != receipt.receipt_id
            or original.test_mode is not False or not _amount(original.paid_kopecks)
            or not _amount(original.refunded_kopecks)
            or original.paid_kopecks <= original.refunded_kopecks
            or not isinstance(original.status, str) or original.status not in {'PAID', 'PARTIAL_REFUND'}
            or (original.refunded_kopecks == 0) != (original.status == 'PAID')
            or original.paid_kopecks - original.refunded_kopecks != receipt.verified_net_kopecks):
        return None, 'BLOCKED_PAYMENT_RECONCILIATION'
    return OriginalPaymentRefund(receipt.receipt_id, original.bank_payment_id,
                                 original.bank_order_id, receipt.verified_net_kopecks), None


def evaluate_cancellation_deposit(slot, notice, receipts, *, checked_at):
    """Calculate eligibility using persisted server facts, with no side effects.

    ``REFUND_READY`` is a plan to request refunds of original payments, never
    evidence of a completed refund. Any missing/uncertain origin blocks the whole
    plan; a manual approved receipt by itself cannot create a new bank transfer.
    Declared booking.deposit is deliberately not an input. All amounts are exact
    kopecks of approved DEPOSIT receipts net of bank-confirmed refunds.
    """
    _validate_facts(slot, notice, checked_at)
    notice_day = notice.received_at.astimezone(MOSCOW).date()
    category = ('FORCE_MAJEURE' if notice.force_majeure else
                'EARLY' if notice_day < slot.shoot_date else 'LATE')
    confirmed = _confirmed_deposits(receipts)
    total = sum(receipt.verified_net_kopecks for receipt in confirmed)
    if not _amount(total):
        raise DepositPolicyError('The verified deposit total exceeds the accounting amount range.')
    refunds, blocked = [], None
    if not total:
        disposition, retained, eligible = 'NO_VERIFIED_DEPOSIT', 0, 0
    elif category == 'LATE':
        disposition, retained, eligible = 'RETAINED', total, 0
    else:
        seen_bank_ids, seen_order_ids = set(), set()
        for receipt in confirmed:
            refund, failure = _original_refund(receipt, slot.booking_id)
            if failure:
                blocked = blocked or failure
                continue
            if refund.bank_payment_id in seen_bank_ids or refund.bank_order_id in seen_order_ids:
                blocked = 'BLOCKED_PAYMENT_RECONCILIATION'
                continue
            seen_bank_ids.add(refund.bank_payment_id)
            seen_order_ids.add(refund.bank_order_id)
            refunds.append(refund)
        disposition, retained, eligible = blocked or 'REFUND_READY', 0, total
        if blocked:
            refunds = []
    return CancellationDepositDecision(POLICY_VERSION, category, notice_day,
        slot.shoot_date, notice.reason.strip(), disposition, total, retained,
        eligible, tuple(refunds))
