"""Opt-in banking facts/refunds; no routes, startup hooks or automatic dispatch.

The owner must install the separate reviewed schema, reservation bindings and
transactional MoneyVersionHooks. Existing aggregate get_status results are NOT
accepted as operation evidence. Nothing here makes an OCR/manual receipt a bank
operation, chooses another recipient, or treats HTTP 200 as a completed refund.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .bank_refund_models import (
    BankRefundIntent,
    OriginalBankPaymentFact,
    PaymentCashEvent,
)
from .models import AuditLog, BankPayment, Base, Booking, Receipt
from .sber_qr_client import SCOPE_STATUS, QRClient, _endpoints
from .services.booking_deposit_policy import OriginalBankPayment

MAX_MONEY = 99_999_999_999_999
SCOPE_CANCEL = 'https://api.sberbank.ru/qr/order.cancel'
_SEAL = object()
_HEX = re.compile(r'[a-f0-9]{64}\Z')
_ID = re.compile(r'[a-zA-Z0-9_\-\\]+\Z')
_RESERVED = {'READY', 'SUBMITTING', 'UNKNOWN', 'PENDING', 'FAILED', 'RECONCILIATION_REQUIRED'}


class RefundError(ValueError):
    """Static safe code only; do not attach raw bank or database exceptions."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _amount(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= MAX_MONEY:
        raise RefundError('refund_amount_invalid')
    return value


def _minor(value):
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError
        return _amount(int(amount), zero=True)
    except (ValueError, TypeError, ArithmeticError):
        raise RefundError('refund_amount_invalid') from None


def _id(value, maximum):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not _ID.fullmatch(value):
        raise RefundError('refund_identifier_invalid')
    return value


def _uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise RefundError('refund_binding_invalid') from None


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RefundError('refund_timestamp_invalid')
    return value.astimezone(UTC)


def _wire_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        raise RefundError('refund_timestamp_invalid')
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
    except ValueError:
        raise RefundError('refund_timestamp_invalid') from None


def _metadata(bill):
    try:
        data = json.loads(bill.pricing_snapshot)
        metadata = data['_bank']
        if (metadata['provider'] != 'sber_qr' or metadata['environment'] != 'production'
                or bill.test_mode is not False or not _HEX.fullmatch(metadata['binding'])):
            raise ValueError
        return metadata
    except (ValueError, TypeError, KeyError):
        raise RefundError('refund_original_bank_binding_unavailable') from None


@dataclass(frozen=True)
class RefundCapabilities:
    """Explicit deployment evidence, never inferred from OAuth/config presence."""
    enabled: bool = False
    working_order_access: bool = False
    working_cancel_access: bool = False
    original_payer_guaranteed: bool = False
    exact_refund_correlation: bool = False
    request_lookup_confirmed: bool = False
    success_codes_confirmed: bool = False
    contract_version: str = ''

    def require_order_evidence(self):
        if (self.working_order_access is not True or self.success_codes_confirmed is not True
                or not isinstance(self.contract_version, str) or not self.contract_version.strip()):
            raise RefundError('refund_working_order_access_unconfirmed')

    def require_submission(self):
        self.require_order_evidence()
        flags = (self.enabled, self.working_cancel_access, self.original_payer_guaranteed,
                 self.exact_refund_correlation, self.request_lookup_confirmed,
                 self.success_codes_confirmed)
        if not all(value is True for value in flags) or not self.contract_version.strip():
            raise RefundError('refund_capability_unconfirmed')

    def require_reconciliation(self):
        self.require_order_evidence()
        if self.exact_refund_correlation is not True or self.request_lookup_confirmed is not True:
            raise RefundError('refund_reconciliation_capability_unconfirmed')


@dataclass(frozen=True)
class OrderIdentity:
    bank_payment_id: int
    bank_order_id: str
    order_number: str
    amount_kopecks: int
    merchant_binding_hash: str


@dataclass(frozen=True)
class PayOperation:
    operation_id: str
    auth_code: str
    rrn: str
    amount_kopecks: int
    operation_at: datetime


@dataclass(frozen=True)
class RefundOperation(PayOperation):
    operation_type: str


@dataclass(frozen=True)
class VerifiedBankEvidence:
    identity: OrderIdentity
    provider: str
    environment: str
    member_id: str
    id_qr: str
    tid: str
    currency: str
    status: str
    payments: tuple[PayOperation, ...]
    refunds: tuple[RefundOperation, ...]
    refunded_kopecks: int
    observed_at: datetime
    evidence_hash: str
    verification_version: str
    _seal: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class RefundRequest:
    intent_id: str
    bank_payment_id: int
    bank_order_id: str
    order_number: str
    order_amount_kopecks: int
    original_operation_id: str
    auth_code: str
    id_qr: str
    tid: str
    amount_kopecks: int
    currency: str
    merchant_binding_hash: str
    rq_uid: str
    rq_tm: str
    payload: str
    payload_hash: str


@dataclass(frozen=True)
class ExactRefundMatch:
    """Output of an explicitly installed, bank-confirmed correlation contract.

    A status aggregate, amount/time delta or response.operation_id without an
    established original/refund meaning cannot construct this contract result.
    """
    bank_order_id: str
    original_operation_id: str
    refund_operation_id: str
    rq_uid: str
    amount_kopecks: int
    currency: str
    operation_at: datetime | None


@dataclass(frozen=True)
class VerifiedRefundEvidence:
    request: RefundRequest
    match: ExactRefundMatch
    operation: RefundOperation
    observed_at: datetime
    evidence_hash: str
    verification_version: str
    _seal: object = field(repr=False, compare=False)


class MoneyVersionHooks(Protocol):
    """All methods are DB-only in the caller's transaction; never commit/I/O.

    after_money_change owns business-case re-evaluation/spendable projection.
    It MUST include the original reservation, even after restore/reschedule.
    No default/no-op hooks are installed by this module.
    """
    async def read_version(self, session, booking) -> int: ...
    async def bump_version(self, session, booking) -> int: ...
    async def after_money_change(self, session, booking_id: int, reservation_key: str | None) -> None: ...


@dataclass(frozen=True)
class MoneySnapshot:
    money_version: int
    reservation_key: str
    net_confirmed_kopecks: int
    refund_reserved_kopecks: int
    cash_event_balance_kopecks: int
    reconciliation_required: bool


@dataclass
class LockedMoneyContext:
    booking: Booking
    bills: dict[int, BankPayment]
    receipts: dict[int, Receipt]
    cases: dict[str, dict]
    intents: dict[str, BankRefundIntent]


class QRRefundAdapter:
    """Strict detail reader plus gated one-shot submit; no money projection."""
    def __init__(self, client: QRClient, *, capabilities=None,
                 exact_refund_verifier: Callable[[dict, RefundRequest], ExactRefundMatch] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.client = client
        self.capabilities = capabilities or RefundCapabilities()
        self.exact_refund_verifier = exact_refund_verifier
        self.clock = clock or (lambda: datetime.now(UTC))

    def _binding(self):
        c = self.client.config
        return _hash([c.environment, c.client_id, c.member_id, c.id_qr, c.tid])

    def require_submission(self):
        self.capabilities.require_submission()
        if self.exact_refund_verifier is None:
            raise RefundError('refund_correlation_contract_missing')
        if self.client.config.environment != 'production':
            raise RefundError('refund_production_original_required')

    def require_reconciliation(self):
        self.capabilities.require_reconciliation()
        if self.exact_refund_verifier is None:
            raise RefundError('refund_correlation_contract_missing')
        if self.client.config.environment != 'production':
            raise RefundError('refund_production_original_required')

    async def read_verified_order(self, identity: OrderIdentity):
        self.capabilities.require_order_evidence()
        c = self.client.config
        if (c.environment != 'production' or self._binding() != identity.merchant_binding_hash
                or not _HEX.fullmatch(identity.merchant_binding_hash)):
            raise RefundError('refund_original_bank_binding_unavailable')
        _id(identity.bank_order_id, 36)
        _amount(identity.amount_kopecks)
        if (not isinstance(identity.order_number, str) or not 1 <= len(identity.order_number) <= 36
                or any(ch in identity.order_number for ch in '\r\n\t\x00')):
            raise RefundError('refund_identifier_invalid')
        # QRClient._call enforces verified mTLS, exact rq_uid/timestamp and the
        # configured bank success code. Unlike get_status, retain operation IDs.
        data = await self.client._call('status', SCOPE_STATUS, {
            'tid': c.tid, 'partner_order_number': identity.order_number,
            'order_id': identity.bank_order_id})
        return self._verify_order(identity, data)

    def _verify_order(self, identity, data):
        c = self.client.config
        if (data.get('order_id') != identity.bank_order_id or data.get('tid') != c.tid
                or data.get('id_qr') != c.id_qr
                or data.get('order_state') not in {'PAID', 'REFUNDED', 'REVERSED'}):
            raise RefundError('refund_order_evidence_mismatch')
        operations = data.get('order_operation_params')
        if not isinstance(operations, list) or not 1 <= len(operations) <= 1000:
            raise RefundError('refund_operation_evidence_missing')
        seen, pays, refunds, refunded = set(), [], [], 0
        normalized = []
        for op in operations:
            if not isinstance(op, dict) or op.get('operation_type') not in {'PAY', 'REFUND', 'REVERSE'}:
                raise RefundError('refund_operation_evidence_invalid')
            oid = _id(op.get('operation_id'), 50)
            if oid in seen or op.get('operation_currency') != '643':
                raise RefundError('refund_operation_evidence_invalid')
            seen.add(oid)
            auth, rrn = _id(op.get('auth_code'), 8), _id(op.get('rrn'), 12)
            amount, at = _amount(op.get('operation_sum'), zero=True), _wire_time(op.get('operation_date_time'))
            if not isinstance(op.get('response_code'), str) or not re.fullmatch(r'[A-Za-z0-9_\-]{1,2}', op['response_code']):
                raise RefundError('refund_operation_evidence_invalid')
            if op['response_code'] != c.operation_success_code:
                continue
            if amount == 0:
                raise RefundError('refund_operation_evidence_invalid')
            normalized.append([oid, op['operation_type'], auth, rrn, amount, at.isoformat()])
            if op['operation_type'] == 'PAY':
                pays.append(PayOperation(oid, auth, rrn, amount, at))
            else:
                refunded += amount
                refunds.append(RefundOperation(oid, auth, rrn, amount, at, op['operation_type']))
        if sum(p.amount_kopecks for p in pays) != identity.amount_kopecks or not 0 <= refunded <= identity.amount_kopecks:
            raise RefundError('refund_operation_coverage_mismatch')
        if data['order_state'] in {'REFUNDED', 'REVERSED'} and not refunded:
            raise RefundError('refund_operation_coverage_mismatch')
        return VerifiedBankEvidence(identity, 'sber_qr', 'production', c.member_id, c.id_qr, c.tid,
            '643', data['order_state'], tuple(sorted(pays, key=lambda p: p.operation_id)),
            tuple(sorted(refunds, key=lambda p: p.operation_id)), refunded,
            _utc(self.clock()), _hash([identity.bank_order_id, sorted(normalized)]),
            'qr-v3-operation-evidence-v1', _SEAL)

    async def submit(self, request: RefundRequest):
        self.require_submission()
        if request.merchant_binding_hash != self._binding():
            raise RefundError('refund_original_bank_binding_unavailable')
        payload = json.loads(request.payload)
        if _hash(payload) != request.payload_hash or payload != _request_payload(request):
            raise RefundError('refund_request_payload_mismatch')
        self.client._ready(for_order=True, require_enabled=False)
        token = await self.client._token(SCOPE_CANCEL)
        api_base, _ = _endpoints(self.client.config)
        data = await self.client._post(api_base + '/qr/order/v3/cancel', headers={
            'RqUID': request.rq_uid, 'Accept': 'application/json',
            'Authorization': 'Bearer ' + token}, body=payload)
        if data.get('rq_uid') != request.rq_uid:
            raise RefundError('refund_response_correlation_mismatch')
        _wire_time(data.get('rq_tm'))
        if data.get('error_code') != self.client.config.success_code:
            # A non-success reply may still need bank lookup; never release the
            # reservation or authorize another submission from this response.
            raise RefundError('refund_submission_unconfirmed')
        return 'PENDING'  # An accepted HTTP/JSON reply is never settlement.

    async def read_verified_refund(self, request: RefundRequest):
        self.require_reconciliation()
        if request.merchant_binding_hash != self._binding():
            raise RefundError('refund_original_bank_binding_unavailable')
        data = await self.client._call('status', SCOPE_STATUS, {
            'tid': request.tid, 'order_id': request.bank_order_id,
            'partner_order_number': request.order_number})
        order = self._verify_order(OrderIdentity(request.bank_payment_id,
            request.bank_order_id, request.order_number, request.order_amount_kopecks,
            request.merchant_binding_hash), data)
        match = self.exact_refund_verifier(data, request)
        if (not isinstance(match, ExactRefundMatch) or match.bank_order_id != request.bank_order_id
                or match.original_operation_id != request.original_operation_id
                or match.rq_uid != request.rq_uid or match.currency != request.currency
                or _amount(match.amount_kopecks) > request.amount_kopecks
                or match.refund_operation_id == request.original_operation_id):
            raise RefundError('refund_exact_correlation_unavailable')
        _id(match.refund_operation_id, 50)
        # Even an installed parent-link verifier cannot make a non-successful
        # or missing refund operation into a confirmed financial event.
        matched = [op for op in data['order_operation_params']
            if op['operation_id'] == match.refund_operation_id
            and op['operation_type'] in {'REFUND', 'REVERSE'}
            and op['response_code'] == self.client.config.operation_success_code
            and op['operation_sum'] == match.amount_kopecks
            and op['operation_currency'] == match.currency]
        if len(matched) != 1 or order.refunded_kopecks < match.amount_kopecks:
            raise RefundError('refund_exact_correlation_unavailable')
        operation = next(op for op in order.refunds if op.operation_id == match.refund_operation_id)
        if match.operation_at is not None and _utc(match.operation_at) != operation.operation_at:
            raise RefundError('refund_operation_date_mismatch')
        match = ExactRefundMatch(match.bank_order_id, match.original_operation_id,
            match.refund_operation_id, match.rq_uid, match.amount_kopecks, match.currency,
            operation.operation_at)
        # The installed verifier must establish response codes, actual refund
        # kind, exact original-operation link and lookup identity. No generic
        # interpretation of optional response.operation_id is supplied here.
        return VerifiedRefundEvidence(request, match, operation, _utc(self.clock()),
            _refund_hash(request, operation),
            self.capabilities.contract_version, _SEAL)


def _request_payload(request):
    return {'rq_uid': request.rq_uid, 'rq_tm': request.rq_tm,
        'order_id': request.bank_order_id, 'operation_id': request.original_operation_id,
        'auth_code': request.auth_code, 'id_qr': request.id_qr, 'tid': request.tid,
        'cancel_operation_sum': request.amount_kopecks, 'operation_currency': request.currency,
        'operation_type': 'REFUND'}


def _db_utc(value):
    # SQLite discards timezone metadata from TIMESTAMPTZ. These columns are
    # written only as UTC; this helper is for stored values, never API input.
    return _utc(value.replace(tzinfo=UTC) if value.tzinfo is None else value)


def _refund_origin(binding, order_id, operation_id):
    return 'refund:' + _hash(['sber_qr', 'production', binding, order_id, operation_id])


def _refund_hash(request, operation):
    return _hash([request.merchant_binding_hash, request.bank_order_id,
        request.original_operation_id, request.rq_uid, operation.operation_id,
        operation.operation_type, operation.auth_code, operation.rrn,
        operation.amount_kopecks, request.currency, operation.operation_at.isoformat()])


def _stored_request(intent, fact, bill):
    return RefundRequest(intent.id, bill.id, fact.bank_order_id, fact.order_number,
        _minor(bill.amount), fact.operation_id, fact.auth_code, fact.id_qr, fact.tid,
        intent.requested_kopecks, fact.currency, fact.merchant_binding_hash,
        intent.rq_uid, intent.rq_tm, intent.request_payload, intent.payload_hash)


class BankRefunds:
    """Owner-integrated DB service; public methods never infer eligibility.

    DB mutation methods use a savepoint and never commit. Their caller owns the
    encompassing transaction. submit_refund alone owns short transactions via
    an explicitly supplied sessionmaker, commits the claim, closes that session,
    and only then performs network I/O. No worker or endpoint is registered.
    """
    def __init__(self, *, hooks: MoneyVersionHooks, adapter: QRRefundAdapter | None = None,
                 clock: Callable[[], datetime] | None = None):
        if hooks is None or any(not callable(getattr(hooks, name, None)) for name in (
                'read_version', 'bump_version', 'after_money_change')):
            raise RefundError('refund_money_hooks_required')
        self.hooks, self.adapter = hooks, adapter
        self.clock = clock or (lambda: datetime.now(UTC))

    async def lock_context(self, session, booking_id, *, case_id=None):
        if type(booking_id) is not int or booking_id <= 0:
            raise RefundError('refund_binding_invalid')
        booking = await session.get(Booking, booking_id, with_for_update=True, populate_existing=True)
        if booking is None:
            raise RefundError('refund_binding_invalid')
        bills = list(await session.scalars(select(BankPayment).where(
            BankPayment.booking_id == booking_id).order_by(BankPayment.id).with_for_update()))
        receipts = list(await session.scalars(select(Receipt).where(
            Receipt.booking_id == booking_id).order_by(Receipt.id).with_for_update()))
        table = self._table('booking_cancellation_cases')
        cases = [dict(r) for r in (await session.execute(select(table).where(
            table.c.booking_id == booking_id).order_by(table.c.id).with_for_update())).mappings()]
        if case_id is not None and _uuid(case_id) not in {str(c['id']) for c in cases}:
            raise RefundError('refund_case_binding_invalid')
        intents = list(await session.scalars(select(BankRefundIntent).where(
            BankRefundIntent.booking_id == booking_id).order_by(BankRefundIntent.id).with_for_update()))
        return LockedMoneyContext(booking, {b.id: b for b in bills}, {r.id: r for r in receipts},
            {str(c['id']): c for c in cases}, {str(i.id): i for i in intents})

    @staticmethod
    def _table(name):
        table = Base.metadata.tables.get(name)
        if table is None:
            raise RefundError('refund_business_schema_not_integrated')
        return table

    async def _reservation(self, session, receipt, bill=None):
        version = getattr(receipt, 'reservation_version_id', None)
        if not version or bill is not None and getattr(bill, 'reservation_version_id', None) != version:
            raise RefundError('refund_original_reservation_unavailable')
        table = self._table('booking_reservation_versions')
        row = (await session.execute(select(table).where(table.c.id == _uuid(str(version)),
            table.c.booking_id == receipt.booking_id))).mappings().one_or_none()
        if row is None:
            raise RefundError('refund_original_reservation_unavailable')
        return str(row['id']), str(row['reservation_key'])

    async def _version(self, session, context, expected=None):
        value = await self.hooks.read_version(session, context.booking)
        if type(value) is not int or value < 0:
            raise RefundError('refund_money_version_unavailable')
        if expected is not None and (type(expected) is not int or expected != value):
            raise RefundError('refund_money_version_conflict')
        return value

    async def _changed(self, session, context, reservation_key, before):
        value = await self.hooks.bump_version(session, context.booking)
        if value != before + 1 or await self._version(session, context) != value:
            raise RefundError('refund_money_version_not_persisted')
        await self.hooks.after_money_change(session, context.booking.id, reservation_key)
        await session.flush()

    @staticmethod
    def _receipt(context, bill):
        receipt = context.receipts.get(bill.receipt_id)
        if receipt is None or receipt.booking_id != bill.booking_id:
            raise RefundError('refund_linked_receipt_unavailable')
        return receipt

    async def _origin_history(self, session, context, bill):
        """One common exact-origin check for claim, policy and settlement.

        Existing mutable Receipt/bill rows are projections of this complete
        durable history; a changed projection is never overwritten silently.
        """
        metadata = _metadata(bill)
        snapshot = json.loads(bill.pricing_snapshot)
        if '_bankSettlementReview' in snapshot or '_refundReview' in snapshot:
            raise RefundError('refund_original_history_drift')
        receipt = self._receipt(context, bill)
        version, key = await self._reservation(session, receipt, bill)
        facts = list(await session.scalars(select(OriginalBankPaymentFact).where(
            OriginalBankPaymentFact.bank_payment_id == bill.id)))
        if not facts:
            raise RefundError('refund_verified_operation_evidence_required')
        by_id = {str(f.id): f for f in facts}
        if any(f.booking_id != context.booking.id or f.receipt_id != receipt.id
                or f.provider != 'sber_qr' or f.environment != 'production'
                or f.bank_order_id != bill.bank_order_id or f.order_number != bill.order_number
                or f.merchant_binding_hash != metadata['binding'] or f.currency != '643'
                or str(f.reservation_version_id) != version or str(f.reservation_key) != key for f in facts):
            raise RefundError('refund_original_history_drift')
        gross = sum(f.amount_kopecks for f in facts)
        if gross != _minor(bill.amount):
            raise RefundError('refund_original_history_drift')
        events = list(await session.scalars(select(PaymentCashEvent).where(
            PaymentCashEvent.receipt_id == receipt.id)))
        pay_events = {e.origin_key: e for e in events if e.origin_type == 'BANK_PAY'}
        if (any(e.origin_type == 'MANUAL_RECEIPT' or e.booking_id != context.booking.id
                or str(e.original_pay_fact_id) not in by_id for e in events)
                or set(pay_events) != {'pay:' + str(f.id) for f in facts}):
            raise RefundError('refund_original_cash_origin_conflict')
        for fact in facts:
            event = pay_events['pay:' + str(fact.id)]
            if (event.amount_kopecks != fact.amount_kopecks or event.currency != fact.currency
                    or event.evidence_hash != fact.evidence_hash or event.refund_intent_id is not None
                    or _db_utc(event.occurred_at) != _db_utc(fact.operation_at)):
                raise RefundError('refund_original_history_drift')
        refund_events = [e for e in events if e.origin_type == 'BANK_REFUND']
        refunded = -sum(e.amount_kopecks for e in refund_events)
        for fact in facts:
            applied = -sum(e.amount_kopecks for e in refund_events if str(e.original_pay_fact_id) == str(fact.id))
            if not 0 <= applied <= fact.amount_kopecks:
                raise RefundError('refund_original_history_drift')
        for event in refund_events:
            intent = context.intents.get(str(event.refund_intent_id))
            if (intent is None or str(intent.original_pay_fact_id) != str(event.original_pay_fact_id)
                    or intent.bank_payment_id != bill.id or intent.receipt_id != receipt.id
                    or intent.booking_id != context.booking.id or event.amount_kopecks >= 0):
                raise RefundError('refund_original_history_drift')
        for intent in context.intents.values():
            if str(intent.original_pay_fact_id) in by_id:
                confirmed = -sum(e.amount_kopecks for e in refund_events if str(e.refund_intent_id) == str(intent.id))
                if confirmed != intent.confirmed_kopecks:
                    raise RefundError('refund_original_history_drift')
        net = gross - refunded
        status = 'PAID' if not refunded else 'PARTIAL_REFUND' if net else 'REFUNDED'
        if (not 0 <= net <= gross or bill.status != status
                or (net and (receipt.status != 'APPROVED' or _minor(receipt.verified_amount) != net))
                or (not net and (receipt.status != 'REJECTED' or receipt.verified_amount is not None))):
            raise RefundError('refund_original_history_drift')
        return facts, receipt, key, refund_events

    async def _review(self, session, context, bill, reason, evidence_hash, *, refunded_kopecks=None):
        data = json.loads(bill.pricing_snapshot)
        marker = {'reason': reason, 'evidenceHash': evidence_hash}
        if refunded_kopecks is not None:
            marker['verifiedReportedRefundedKopecks'] = _amount(refunded_kopecks, zero=True)
        if data.get('_refundReview') == marker:
            return
        before = await self._version(session, context)
        data['_refundReview'] = marker
        bill.pricing_snapshot, bill.status = _json(data), 'RECONCILIATION_REQUIRED'
        for intent in context.intents.values():
            if intent.bank_payment_id == bill.id and intent.state != 'CONFIRMED':
                intent.state, intent.last_error_code = 'RECONCILIATION_REQUIRED', reason
        session.add(AuditLog(action='bank_refund_requires_review', entity='bank_payment',
            entity_id=bill.id, details=_json(marker)))
        receipt = context.receipts.get(bill.receipt_id)
        key = None
        if receipt is not None:
            try:
                _, key = await self._reservation(session, receipt, bill)
            except RefundError:
                pass  # Preserve the verified ambiguity without inventing legacy facts.
        await self._changed(session, context, key, before)

    async def apply_verified_bank_evidence(self, session: AsyncSession, *, booking_id: int,
            bill_id: int, evidence: VerifiedBankEvidence, expected_money_version: int):
        if not isinstance(evidence, VerifiedBankEvidence) or evidence._seal is not _SEAL:
            raise RefundError('refund_verified_operation_evidence_required')
        async with session.begin_nested():
            context = await self.lock_context(session, booking_id)
            before = await self._version(session, context, expected_money_version)
            bill = context.bills.get(bill_id)
            if bill is None:
                raise RefundError('refund_binding_invalid')
            metadata = _metadata(bill)
            identity = evidence.identity
            if (identity.bank_payment_id != bill.id or identity.bank_order_id != bill.bank_order_id
                    or identity.order_number != bill.order_number or identity.amount_kopecks != _minor(bill.amount)
                    or metadata['binding'] != identity.merchant_binding_hash or evidence.provider != 'sber_qr'
                    or evidence.environment != 'production' or evidence.currency != '643'):
                raise RefundError('refund_order_evidence_mismatch')
            review = json.loads(bill.pricing_snapshot).get('_refundReview')
            if isinstance(review, dict) and review.get('evidenceHash') == evidence.evidence_hash:
                return ()  # Same verified ambiguous observation is not another audit/event.
            receipt = self._receipt(context, bill)
            version, key = await self._reservation(session, receipt, bill)
            old = list(await session.scalars(select(OriginalBankPaymentFact).where(
                OriginalBankPaymentFact.bank_payment_id == bill.id)))
            signatures = {p.operation_id: (p.auth_code, p.rrn, p.amount_kopecks, p.operation_at)
                          for p in evidence.payments}
            if (old and signatures != {f.operation_id: (f.auth_code, f.rrn, f.amount_kopecks,
                    _db_utc(f.operation_at)) for f in old}):
                await self._review(session, context, bill, 'refund_pay_history_changed', evidence.evidence_hash,
                    refunded_kopecks=evidence.refunded_kopecks)
                return ()
            events = []
            if old:
                try:
                    _, _, _, events = await self._origin_history(session, context, bill)
                except RefundError:
                    await self._review(session, context, bill, 'refund_original_history_drift', evidence.evidence_hash)
                    return ()
            known_refunds = -sum(e.amount_kopecks for e in events)
            if evidence.refunded_kopecks != known_refunds:
                await self._review(session, context, bill, 'refund_exact_history_unavailable', evidence.evidence_hash,
                    refunded_kopecks=evidence.refunded_kopecks)
                return ()
            if old:
                by_origin = {e.origin_key: e for e in events}
                operation_origins = {_refund_origin(metadata['binding'], bill.bank_order_id, op.operation_id): op
                    for op in evidence.refunds}
                exact = set(by_origin) == set(operation_origins)
                for origin, op in operation_origins.items():
                    event = by_origin.get(origin)
                    intent = context.intents.get(str(event.refund_intent_id)) if event else None
                    fact = next((f for f in old if str(f.id) == str(intent.original_pay_fact_id)), None) if intent else None
                    if (event is None or intent is None or fact is None
                            or event.amount_kopecks != -op.amount_kopecks
                            or event.evidence_hash != _refund_hash(_stored_request(intent, fact, bill), op)):
                        exact = False
                if not exact:
                    await self._review(session, context, bill, 'refund_exact_history_unavailable', evidence.evidence_hash,
                        refunded_kopecks=evidence.refunded_kopecks)
                    return ()
                # Never restore Receipt NET from a stale PAID aggregate.
                return tuple(f.id for f in old)
            if await session.scalar(select(PaymentCashEvent.id).where(
                    PaymentCashEvent.receipt_id == receipt.id).limit(1)):
                await self._review(session, context, bill, 'refund_original_cash_origin_conflict', evidence.evidence_hash)
                return ()
            if (bill.status == 'RECONCILIATION_REQUIRED' or '_bankSettlementReview' in json.loads(bill.pricing_snapshot)
                    or receipt.status != 'APPROVED' or _minor(receipt.verified_amount) != identity.amount_kopecks):
                raise RefundError('refund_original_receipt_reconciliation_required')
            if (not signatures or sum(p.amount_kopecks for p in evidence.payments) != identity.amount_kopecks
                    or len(signatures) != len(evidence.payments)):
                raise RefundError('refund_operation_coverage_mismatch')
            facts = []
            for op in evidence.payments:
                fact = OriginalBankPaymentFact(id=str(uuid4()), booking_id=booking_id,
                    bank_payment_id=bill.id, receipt_id=receipt.id, reservation_version_id=version,
                    reservation_key=key, provider=evidence.provider, environment=evidence.environment,
                    merchant_binding_hash=identity.merchant_binding_hash, bank_order_id=identity.bank_order_id,
                    order_number=identity.order_number, operation_id=op.operation_id, auth_code=op.auth_code,
                    rrn=op.rrn, member_id=evidence.member_id, id_qr=evidence.id_qr, tid=evidence.tid,
                    amount_kopecks=op.amount_kopecks, currency=evidence.currency,
                    operation_at=op.operation_at, observed_at=evidence.observed_at,
                    evidence_hash=evidence.evidence_hash, verification_version=evidence.verification_version)
                session.add(fact)
                await session.flush()
                self._cash(session, fact=fact, receipt=receipt, intent=None, amount=op.amount_kopecks,
                    origin_type='BANK_PAY', origin_key='pay:' + fact.id, at=op.operation_at,
                    observed=evidence.observed_at, evidence_hash=evidence.evidence_hash,
                    verification_version=evidence.verification_version)
                facts.append(fact)
            await self._changed(session, context, key, before)
            return tuple(f.id for f in facts)

    record_original_payment = apply_verified_bank_evidence

    @staticmethod
    def _cash(session, *, fact, receipt, intent, amount, origin_type, origin_key, at,
              observed, evidence_hash, verification_version):
        event = PaymentCashEvent(id=str(uuid4()), booking_id=receipt.booking_id, receipt_id=receipt.id,
            original_pay_fact_id=fact.id if fact else None, refund_intent_id=intent.id if intent else None,
            kind='REFUND' if amount < 0 else 'PAY', origin_type=origin_type, origin_key=origin_key,
            amount_kopecks=amount, currency='643', occurred_at=at or observed, observed_at=observed,
            date_source=('MANUAL_REVIEW' if origin_type == 'MANUAL_RECEIPT' else
                'BANK_OPERATION' if at else 'OBSERVED_AT'), evidence_hash=evidence_hash,
            verification_version=verification_version)
        session.add(event)
        return event

    async def load_original_payments_for_policy(self, session, *, booking_id, reservation_key):
        context = await self.lock_context(session, booking_id)
        key = _uuid(reservation_key)
        facts = list(await session.scalars(select(OriginalBankPaymentFact).where(
            OriginalBankPaymentFact.booking_id == booking_id, OriginalBankPaymentFact.reservation_key == key)))
        by_bill = {}
        for fact in facts:
            by_bill.setdefault(fact.bank_payment_id, []).append(fact)
        result = []
        for bid, group in by_bill.items():
            bill = context.bills.get(bid)
            if bill is None or bill.status not in {'PAID', 'PARTIAL_REFUND', 'REFUNDED'}:
                raise RefundError('refund_original_receipt_reconciliation_required')
            metadata = _metadata(bill)
            receipt = self._receipt(context, bill)
            await self._origin_history(session, context, bill)
            _, actual_key = await self._reservation(session, receipt, bill)
            if actual_key != key or any(f.receipt_id != receipt.id or f.merchant_binding_hash != metadata['binding'] for f in group):
                raise RefundError('refund_binding_invalid')
            paid = sum(f.amount_kopecks for f in group)
            refunded = -sum((await session.scalars(select(PaymentCashEvent.amount_kopecks).where(
                PaymentCashEvent.original_pay_fact_id.in_([f.id for f in group]),
                PaymentCashEvent.origin_type == 'BANK_REFUND'))).all())
            if paid != _minor(bill.amount) or refunded > paid:
                raise RefundError('refund_operation_coverage_mismatch')
            net = paid - refunded
            if net and (receipt.status != 'APPROVED' or _minor(receipt.verified_amount) != net):
                raise RefundError('refund_original_receipt_reconciliation_required')
            result.append(OriginalBankPayment(bid, bill.bank_order_id, booking_id, receipt.id,
                paid, refunded, 'REFUNDED' if not net else 'PARTIAL_REFUND' if refunded else 'PAID', False))
        return tuple(result)

    async def prepare_refund_intents(self, session, *, booking_id, case_id, request_key,
            expected_money_version, amounts: dict[str, int]):
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 40 or not _ID.fullmatch(request_key):
            raise RefundError('refund_request_key_invalid')
        if not isinstance(amounts, dict) or not amounts:
            raise RefundError('refund_amount_invalid')
        allocations = {_uuid(fid): _amount(amount) for fid, amount in amounts.items()}
        async with session.begin_nested():
            context = await self.lock_context(session, booking_id, case_id=case_id)
            case = context.cases[_uuid(case_id)]
            key = str(case['reservation_key']) if case['reservation_key'] else None
            if key is None:
                raise RefundError('refund_original_reservation_unavailable')
            facts = list(await session.scalars(select(OriginalBankPaymentFact).where(
                OriginalBankPaymentFact.booking_id == booking_id,
                OriginalBankPaymentFact.reservation_key == key)))
            eligible = {f.id: f for f in facts}
            if set(allocations) - eligible.keys():
                raise RefundError('refund_binding_invalid')
            existing = [i for i in context.intents.values() if str(i.case_id) == _uuid(case_id)]
            if existing:
                original_allocations = {str(i.original_pay_fact_id): i.requested_kopecks for i in existing}
                if (not original_allocations.keys() <= allocations.keys()
                        or any(allocations[fid] != amount for fid, amount in original_allocations.items())
                        or any(i.request_key != request_key + ':' + str(i.original_pay_fact_id) for i in existing)):
                    raise RefundError('refund_request_payload_conflict')
                if original_allocations == allocations:
                    return tuple(i.id for i in existing)
            before = await self._version(session, context, expected_money_version)
            if case['state'] not in {'REFUND_READY', 'REFUND_PENDING', 'REFUND_UNKNOWN'}:
                raise RefundError('refund_business_eligibility_required')
            if sum(allocations.values()) != case['eligible_refund_kopecks']:
                if existing:
                    raise RefundError('refund_case_replan_required')
                raise RefundError('refund_case_amount_mismatch')
            # The whole reservation must have a verified original for every
            # eligible approved deposit; a linked subset cannot bypass split
            # deposit/manual-origin rejection.
            originals = await self.load_original_payments_for_policy(session,
                booking_id=booking_id, reservation_key=key)
            covered = {o.receipt_id for o in originals if o.paid_kopecks > o.refunded_kopecks}
            for receipt in context.receipts.values():
                if receipt.purpose == 'DEPOSIT' and receipt.status == 'APPROVED':
                    _, rkey = await self._reservation(session, receipt)
                    if rkey == key and receipt.id not in covered:
                        raise RefundError('refund_original_bank_binding_unavailable')
            created = list(existing)
            for fid, amount in sorted(allocations.items()):
                if any(str(i.original_pay_fact_id) == fid for i in existing):
                    continue  # Its immutable payload/one-shot claim is retained.
                fact = eligible[fid]
                receipt = context.receipts.get(fact.receipt_id)
                bill = context.bills.get(fact.bank_payment_id)
                if receipt is None or bill is None or bill.receipt_id != receipt.id or receipt.purpose != 'DEPOSIT':
                    raise RefundError('refund_binding_invalid')
                if _metadata(bill)['binding'] != fact.merchant_binding_hash or bill.status == 'RECONCILIATION_REQUIRED':
                    raise RefundError('refund_original_bank_binding_unavailable')
                confirmed = -sum((await session.scalars(select(PaymentCashEvent.amount_kopecks).where(
                    PaymentCashEvent.original_pay_fact_id == fid,
                    PaymentCashEvent.origin_type == 'BANK_REFUND'))).all())
                reserved = sum(i.requested_kopecks - i.confirmed_kopecks for i in context.intents.values()
                    if str(i.original_pay_fact_id) == fid and i.state in _RESERVED)
                if confirmed + reserved + amount > fact.amount_kopecks:
                    raise RefundError('refund_amount_already_reserved')
                iid, uid = str(uuid4()), uuid4().hex
                now = _utc(self.clock())
                request = RefundRequest(iid, bill.id, fact.bank_order_id, fact.order_number,
                    _minor(bill.amount), fact.operation_id,
                    fact.auth_code, fact.id_qr, fact.tid, amount, fact.currency,
                    fact.merchant_binding_hash, uid, now.strftime('%Y-%m-%dT%H:%M:%SZ'), '', '')
                payload = _request_payload(request)
                intent = BankRefundIntent(id=iid, booking_id=booking_id, bank_payment_id=bill.id,
                    receipt_id=receipt.id, case_id=_uuid(case_id), original_pay_fact_id=fid,
                    purpose='BOOKING_CANCELLATION', request_key=request_key + ':' + fid,
                    payload_hash=_hash(payload), rq_uid=uid, rq_tm=request.rq_tm,
                    request_payload=_json(payload), requested_kopecks=amount,
                    baseline_refunded_kopecks=confirmed, confirmed_kopecks=0, state='READY',
                    attempt_count=0, created_at=now)
                session.add(intent)
                created.append(intent)
            await self._changed(session, context, key, before)
            return tuple(i.id for i in created)

    async def _request(self, session, context, intent):
        fact = await session.get(OriginalBankPaymentFact, intent.original_pay_fact_id)
        bill, receipt = context.bills.get(intent.bank_payment_id), context.receipts.get(intent.receipt_id)
        if (fact is None or bill is None or receipt is None or bill.receipt_id != receipt.id
                or fact.booking_id != context.booking.id or fact.receipt_id != receipt.id
                or fact.bank_payment_id != bill.id or str(intent.case_id) not in context.cases
                or _metadata(bill)['binding'] != fact.merchant_binding_hash
                or bill.status == 'RECONCILIATION_REQUIRED'):
            raise RefundError('refund_binding_invalid')
        case = context.cases[str(intent.case_id)]
        if str(case['reservation_key']) != str(fact.reservation_key):
            raise RefundError('refund_binding_invalid')
        version, key = await self._reservation(session, receipt, bill)
        if version != str(fact.reservation_version_id) or key != str(fact.reservation_key):
            raise RefundError('refund_binding_invalid')
        await self._origin_history(session, context, bill)
        return _stored_request(intent, fact, bill)

    async def claim_refund_submission(self, session, *, booking_id, intent_id, expected_money_version):
        if self.adapter is None:
            raise RefundError('refund_capability_unconfirmed')
        self.adapter.require_submission()
        async with session.begin_nested():
            context = await self.lock_context(session, booking_id)
            before = await self._version(session, context, expected_money_version)
            intent = context.intents.get(_uuid(intent_id))
            if intent is None:
                raise RefundError('refund_binding_invalid')
            if intent.attempt_count != 0 or intent.state != 'READY':
                raise RefundError('refund_submission_already_claimed')
            if context.cases[str(intent.case_id)]['state'] not in {'REFUND_READY', 'REFUND_PENDING', 'REFUND_UNKNOWN'}:
                raise RefundError('refund_business_eligibility_required')
            request = await self._request(session, context, intent)
            if _hash(json.loads(request.payload)) != request.payload_hash or json.loads(request.payload) != _request_payload(request):
                raise RefundError('refund_request_payload_mismatch')
            intent.state, intent.attempt_count, intent.submitted_at = 'SUBMITTING', 1, _utc(self.clock())
            fact = await session.get(OriginalBankPaymentFact, intent.original_pay_fact_id)
            await self._changed(session, context, str(fact.reservation_key), before)
            return request

    async def submit_refund(self, sessions, *, booking_id, intent_id, expected_money_version):
        """Commit exactly one claim before I/O; crash/timeout never resubmits."""
        async with sessions() as session:
            request = await self.claim_refund_submission(session, booking_id=booking_id,
                intent_id=intent_id, expected_money_version=expected_money_version)
            await session.commit()
        try:
            await self.adapter.submit(request)
            state, error = 'PENDING', None
        except Exception:  # noqa: BLE001 -- any post-claim failure leaves an unknown outcome, never retry.
            state, error = 'UNKNOWN', 'refund_submission_outcome_unknown'
        async with sessions() as session, session.begin():
            context = await self.lock_context(session, booking_id)
            intent = context.intents[_uuid(intent_id)]
            # A simultaneous exact status confirmation wins over an older ACK.
            if intent.state == 'SUBMITTING':
                before = await self._version(session, context)
                intent.state, intent.last_error_code = state, error
                intent.checked_at = _utc(self.clock())
                fact = await session.get(OriginalBankPaymentFact, intent.original_pay_fact_id)
                await self._changed(session, context, str(fact.reservation_key), before)
            state = intent.state
        return state

    async def reconcile_refund(self, sessions, *, booking_id, intent_id):
        if self.adapter is None:
            raise RefundError('refund_capability_unconfirmed')
        self.adapter.require_reconciliation()
        async with sessions() as session:
            context = await self.lock_context(session, booking_id)
            intent = context.intents.get(_uuid(intent_id))
            if intent is None or intent.attempt_count != 1:
                raise RefundError('refund_submission_not_claimed')
            request = await self._request(session, context, intent)
        try:
            evidence = await self.adapter.read_verified_refund(request)
        except RefundError:
            async with sessions() as session, session.begin():
                context = await self.lock_context(session, booking_id)
                await self._review(session, context, context.bills[request.bank_payment_id],
                    'refund_exact_correlation_unavailable', request.payload_hash)
            return 'RECONCILIATION_REQUIRED'
        async with sessions() as session, session.begin():
            return await self.apply_verified_refund(session, booking_id=booking_id,
                intent_id=intent_id, evidence=evidence)

    async def apply_verified_refund(self, session, *, booking_id, intent_id, evidence):
        if not isinstance(evidence, VerifiedRefundEvidence) or evidence._seal is not _SEAL:
            raise RefundError('refund_verified_operation_evidence_required')
        async with session.begin_nested():
            context = await self.lock_context(session, booking_id)
            before = await self._version(session, context)
            intent = context.intents.get(_uuid(intent_id))
            if intent is None or intent.attempt_count != 1:
                raise RefundError('refund_submission_not_claimed')
            request = await self._request(session, context, intent)
            if evidence.request != request:
                raise RefundError('refund_request_payload_conflict')
            match = evidence.match
            if (match.rq_uid != request.rq_uid or match.refund_operation_id == request.original_operation_id
                    or match.currency != request.currency or _amount(match.amount_kopecks) > request.amount_kopecks
                    or evidence.operation.operation_id != match.refund_operation_id
                    or evidence.operation.amount_kopecks != match.amount_kopecks
                    or evidence.operation.operation_type not in {'REFUND', 'REVERSE'}
                    or match.operation_at != evidence.operation.operation_at
                    or evidence.evidence_hash != _refund_hash(request, evidence.operation)):
                raise RefundError('refund_exact_correlation_unavailable')
            origin = _refund_origin(request.merchant_binding_hash, match.bank_order_id, match.refund_operation_id)
            prior = await session.scalar(select(PaymentCashEvent).where(
                PaymentCashEvent.origin_key == origin))
            if prior is not None:
                if (str(prior.refund_intent_id) != str(intent.id)
                        or prior.amount_kopecks != -match.amount_kopecks
                        or prior.evidence_hash != evidence.evidence_hash):
                    raise RefundError('refund_operation_identity_conflict')
                return prior.id
            fact = await session.get(OriginalBankPaymentFact, intent.original_pay_fact_id)
            if (intent.confirmed_kopecks + match.amount_kopecks > intent.requested_kopecks
                    or match.original_operation_id != fact.operation_id
                    or match.bank_order_id != fact.bank_order_id or match.currency != fact.currency):
                raise RefundError('refund_operation_coverage_mismatch')
            refunded = -sum((await session.scalars(select(PaymentCashEvent.amount_kopecks).where(
                PaymentCashEvent.original_pay_fact_id == fact.id,
                PaymentCashEvent.origin_type == 'BANK_REFUND'))).all())
            if refunded + match.amount_kopecks > fact.amount_kopecks:
                raise RefundError('refund_operation_coverage_mismatch')
            receipt = context.receipts[intent.receipt_id]
            event = self._cash(session, fact=fact, receipt=receipt, intent=intent,
                amount=-match.amount_kopecks, origin_type='BANK_REFUND', origin_key=origin,
                at=match.operation_at, observed=evidence.observed_at,
                evidence_hash=evidence.evidence_hash, verification_version=evidence.verification_version)
            intent.confirmed_kopecks += match.amount_kopecks
            intent.state = 'CONFIRMED' if intent.confirmed_kopecks == intent.requested_kopecks else 'PENDING'
            intent.checked_at, intent.provider_reference = evidence.observed_at, match.refund_operation_id
            await session.flush()
            # Project the original bill's complete durable history, never the
            # last remote aggregate and never a second/negative Receipt.
            gross = sum((await session.scalars(select(OriginalBankPaymentFact.amount_kopecks).where(
                OriginalBankPaymentFact.bank_payment_id == intent.bank_payment_id))).all())
            total_refunded = -sum((await session.scalars(select(PaymentCashEvent.amount_kopecks).join(
                OriginalBankPaymentFact, OriginalBankPaymentFact.id == PaymentCashEvent.original_pay_fact_id).where(
                OriginalBankPaymentFact.bank_payment_id == intent.bank_payment_id,
                PaymentCashEvent.origin_type == 'BANK_REFUND'))).all())
            net = gross - total_refunded
            if not 0 <= net <= gross:
                raise RefundError('refund_operation_coverage_mismatch')
            receipt.status, receipt.verified_amount = ('APPROVED', Decimal(net) / 100) if net else ('REJECTED', None)
            context.bills[intent.bank_payment_id].status = 'PARTIAL_REFUND' if net else 'REFUNDED'
            session.add(AuditLog(action='bank_refund_confirmed', entity='bank_payment',
                entity_id=intent.bank_payment_id, details=_json({'intentId': intent.id, 'cashEventId': event.id})))
            await self._changed(session, context, str(fact.reservation_key), before)
            return event.id

    async def record_manual_receipt_cash_event(self, session, *, booking_id, receipt_id,
            approval_identity, expected_money_version):
        if not isinstance(approval_identity, str) or not 1 <= len(approval_identity) <= 128:
            raise RefundError('refund_manual_approval_identity_required')
        async with session.begin_nested():
            context = await self.lock_context(session, booking_id)
            receipt = context.receipts.get(receipt_id)
            if (receipt is None or receipt.status != 'APPROVED' or receipt.reviewed_at is None
                    or any(b.receipt_id == receipt_id for b in context.bills.values())):
                raise RefundError('refund_manual_receipt_binding_invalid')
            _, key = await self._reservation(session, receipt)
            amount = _amount(_minor(receipt.verified_amount))
            signature = _hash([receipt_id, amount, approval_identity])
            origin = 'manual-receipt:' + str(receipt_id)
            old = await session.scalar(select(PaymentCashEvent).where(PaymentCashEvent.origin_key == origin))
            if old is not None:
                if old.amount_kopecks != amount or old.evidence_hash != signature:
                    raise RefundError('refund_manual_approval_identity_conflict')
                return old.id
            before = await self._version(session, context, expected_money_version)
            reviewed = receipt.reviewed_at
            # Existing Receipt columns are naive UTC; do not reinterpret them
            # as local/Moscow dates when producing the dated cash event.
            reviewed = reviewed.replace(tzinfo=UTC) if reviewed.tzinfo is None else _utc(reviewed)
            event = self._cash(session, fact=None, receipt=receipt, intent=None, amount=amount,
                origin_type='MANUAL_RECEIPT', origin_key=origin, at=reviewed,
                observed=_utc(self.clock()), evidence_hash=signature,
                verification_version='manual-receipt-approval-v1')
            await self._changed(session, context, key, before)
            return event.id

    async def money_snapshot(self, session, *, booking_id, reservation_key):
        context = await self.lock_context(session, booking_id)
        version = await self._version(session, context)
        key = _uuid(reservation_key)
        matching = []
        for receipt in context.receipts.values():
            if receipt.reservation_version_id is None:
                # Legacy money stays unbound and is held by the owner's credit
                # resolver. It must not be invented as this reservation, nor
                # prevent validation of a later, separately bound agreement.
                continue
            _, rkey = await self._reservation(session, receipt)
            if rkey == key:
                matching.append(receipt)
        events = list(await session.scalars(select(PaymentCashEvent).where(
            PaymentCashEvent.receipt_id.in_([r.id for r in matching]))))
        net, recon = 0, False
        for receipt in matching:
            bills = [b for b in context.bills.values() if b.receipt_id == receipt.id]
            if bills:
                try:
                    if len(bills) != 1:
                        raise RefundError('refund_original_history_drift')
                    await self._origin_history(session, context, bills[0])
                except RefundError:
                    recon = True
                    continue  # Never expose an unverified mutable NET as photo credit.
            elif receipt.status == 'APPROVED':
                manual = [e for e in events if e.receipt_id == receipt.id]
                if (len(manual) != 1 or manual[0].origin_type != 'MANUAL_RECEIPT'
                        or manual[0].amount_kopecks != _minor(receipt.verified_amount)):
                    recon = True
                    continue
            if receipt.status == 'APPROVED':
                net += _minor(receipt.verified_amount)
        facts = list(await session.scalars(select(OriginalBankPaymentFact).where(
            OriginalBankPaymentFact.booking_id == booking_id, OriginalBankPaymentFact.reservation_key == key)))
        ids = {str(f.id) for f in facts}
        reserved = sum(i.requested_kopecks - i.confirmed_kopecks for i in context.intents.values()
            if str(i.original_pay_fact_id) in ids and i.state in _RESERVED)
        cash = sum(e.amount_kopecks for e in events)
        recon = recon or any(b.status == 'RECONCILIATION_REQUIRED' and b.receipt_id in {r.id for r in matching}
                            for b in context.bills.values())
        # No spendable calculation here: owner combines mutually exclusive
        # retained/reserved/reconciliation dispositions, using this snapshot.
        return MoneySnapshot(version, key, net, reserved, cash, recon)
