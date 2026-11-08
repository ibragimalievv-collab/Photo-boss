"""Bank-owned payment evidence, refund intent and dated cash-event models.

Importing this module only registers metadata. It does not migrate a database or
enable provider calls. ORM guards protect these facts from ordinary ORM changes;
the reviewed PostgreSQL migration must also provide direct-SQL immutability
triggers. These events are not additional ``CashMovement`` expenses.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

MAX_MONEY_KOPECKS = 99999999999999


def _aware_utc_now():
    return datetime.now(UTC)


class OriginalBankPaymentFact(Base):
    """One immutable, verified PAY operation with its original money binding."""

    __tablename__ = "bank_original_pay_facts"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), index=True
    )
    bank_payment_id: Mapped[int] = mapped_column(
        ForeignKey("bank_payments.id", ondelete="RESTRICT"), index=True
    )
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="RESTRICT"), index=True
    )
    reservation_version_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("booking_reservation_versions.id", ondelete="RESTRICT"),
    )
    reservation_key: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    provider: Mapped[str] = mapped_column(String(30))
    environment: Mapped[str] = mapped_column(String(20))
    merchant_binding_hash: Mapped[str] = mapped_column(String(64))
    bank_order_id: Mapped[str] = mapped_column(String(100))
    order_number: Mapped[str] = mapped_column(String(64))
    operation_id: Mapped[str] = mapped_column(String(50))
    auth_code: Mapped[str] = mapped_column(String(8))
    rrn: Mapped[str] = mapped_column(String(12))
    member_id: Mapped[str] = mapped_column(String(8))
    id_qr: Mapped[str] = mapped_column(String(36))
    tid: Mapped[str] = mapped_column(String(8))
    amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    operation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_aware_utc_now
    )
    verification_version: Mapped[str] = mapped_column(String(80))
    evidence_hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint(
            "provider", "environment", "merchant_binding_hash", "bank_order_id",
            "operation_id", name="uq_original_bank_pay_operation",
        ),
        CheckConstraint(
            f"amount_kopecks BETWEEN 1 AND {MAX_MONEY_KOPECKS}",
            name="ck_original_bank_pay_amount",
        ),
        CheckConstraint("length(currency) = 3", name="ck_original_bank_pay_currency"),
        CheckConstraint(
            "length(merchant_binding_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_original_bank_pay_hashes",
        ),
        CheckConstraint(
            "length(provider) BETWEEN 1 AND 30 AND "
            "length(environment) BETWEEN 1 AND 20 AND "
            "length(bank_order_id) BETWEEN 1 AND 100 AND "
            "length(order_number) BETWEEN 1 AND 64 AND "
            "length(operation_id) BETWEEN 1 AND 50 AND "
            "length(auth_code) BETWEEN 1 AND 8 AND "
            "length(rrn) BETWEEN 1 AND 12 AND "
            "length(member_id) BETWEEN 1 AND 8 AND "
            "length(id_qr) BETWEEN 1 AND 36 AND "
            "length(tid) BETWEEN 1 AND 8 AND "
            "length(verification_version) BETWEEN 1 AND 80",
            name="ck_original_bank_pay_identifiers",
        ),
    )


class BankRefundIntent(Base):
    """One durable original-payer refund request, never a blind retry queue."""

    __tablename__ = "bank_refund_intents"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), index=True
    )
    bank_payment_id: Mapped[int] = mapped_column(
        ForeignKey("bank_payments.id", ondelete="RESTRICT"), index=True
    )
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="RESTRICT"), index=True
    )
    case_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("booking_cancellation_cases.id", ondelete="RESTRICT"),
        index=True,
    )
    original_pay_fact_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("bank_original_pay_facts.id", ondelete="RESTRICT"),
        index=True,
    )
    purpose: Mapped[str] = mapped_column(
        String(32), default="BOOKING_CANCELLATION"
    )
    request_key: Mapped[str] = mapped_column(String(80), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    rq_uid: Mapped[str] = mapped_column(String(32), unique=True)
    rq_tm: Mapped[str] = mapped_column(String(20))
    # Canonical JSON text avoids unnoticed in-place nested JSON mutations. The
    # service constructs and validates the provider allowlist before insertion.
    request_payload: Mapped[str] = mapped_column(Text)
    requested_kopecks: Mapped[int] = mapped_column(BigInteger)
    baseline_refunded_kopecks: Mapped[int] = mapped_column(BigInteger)
    confirmed_kopecks: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String(32), default="READY", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_aware_utc_now
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_reference: Mapped[str | None] = mapped_column(String(100))
    last_error_code: Mapped[str | None] = mapped_column(String(80))

    __table_args__ = (
        UniqueConstraint(
            "case_id", "original_pay_fact_id", "purpose",
            name="uq_bank_refund_case_original_purpose",
        ),
        CheckConstraint(
            "purpose = 'BOOKING_CANCELLATION'", name="ck_bank_refund_purpose"
        ),
        CheckConstraint(
            f"requested_kopecks BETWEEN 1 AND {MAX_MONEY_KOPECKS} AND "
            f"baseline_refunded_kopecks BETWEEN 0 AND {MAX_MONEY_KOPECKS} AND "
            "confirmed_kopecks BETWEEN 0 AND requested_kopecks",
            name="ck_bank_refund_amounts",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 1", name="ck_bank_refund_one_submission"
        ),
        CheckConstraint(
            "state IN ('READY','SUBMITTING','UNKNOWN','PENDING','CONFIRMED',"
            "'FAILED','RECONCILIATION_REQUIRED')",
            name="ck_bank_refund_state",
        ),
        CheckConstraint(
            "length(request_key) BETWEEN 1 AND 80 AND "
            "length(payload_hash) = 64 AND length(rq_uid) = 32 AND "
            "length(rq_tm) = 20 AND length(request_payload) > 0",
            name="ck_bank_refund_request",
        ),
    )


class PaymentCashEvent(Base):
    """Immutable dated money event; Receipt remains the credit projection."""

    __tablename__ = "payment_cash_events"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), index=True
    )
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="RESTRICT"), index=True
    )
    original_pay_fact_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("bank_original_pay_facts.id", ondelete="RESTRICT"),
        index=True,
    )
    refund_intent_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("bank_refund_intents.id", ondelete="RESTRICT"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(10))
    origin_type: Mapped[str] = mapped_column(String(20))
    origin_key: Mapped[str] = mapped_column(String(256), unique=True)
    amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_aware_utc_now
    )
    date_source: Mapped[str] = mapped_column(String(20))
    verification_version: Mapped[str] = mapped_column(String(80))
    evidence_hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint(
            f"amount_kopecks BETWEEN -{MAX_MONEY_KOPECKS} AND {MAX_MONEY_KOPECKS} "
            "AND amount_kopecks <> 0",
            name="ck_payment_cash_event_amount",
        ),
        CheckConstraint(
            "(kind = 'PAY' AND amount_kopecks > 0 AND refund_intent_id IS NULL "
            "AND ((origin_type = 'BANK_PAY' AND original_pay_fact_id IS NOT NULL) "
            "OR (origin_type = 'MANUAL_RECEIPT' AND original_pay_fact_id IS NULL))) "
            "OR (kind = 'REFUND' AND amount_kopecks < 0 "
            "AND origin_type = 'BANK_REFUND' AND original_pay_fact_id IS NOT NULL "
            "AND refund_intent_id IS NOT NULL)",
            name="ck_payment_cash_event_origin",
        ),
        CheckConstraint(
            "date_source IN ('BANK_OPERATION','OBSERVED_AT','MANUAL_REVIEW')",
            name="ck_payment_cash_event_date_source",
        ),
        CheckConstraint(
            "length(currency) = 3 AND length(origin_key) BETWEEN 1 AND 256 AND "
            "length(verification_version) BETWEEN 1 AND 80 AND "
            "length(evidence_hash) = 64",
            name="ck_payment_cash_event_evidence",
        ),
    )


class BankRefundFactImmutableError(ValueError):
    """Safe error without provider data, identity fields or request payloads."""


_INTENT_IDENTITY_FIELDS = (
    "id", "booking_id", "bank_payment_id", "receipt_id", "case_id",
    "original_pay_fact_id", "purpose", "request_key", "payload_hash", "rq_uid",
    "rq_tm", "request_payload", "requested_kopecks", "baseline_refunded_kopecks",
    "created_at",
)


def _guard_immutable_update(mapper, _connection, target):
    state = inspect(target)
    fields = (
        _INTENT_IDENTITY_FIELDS
        if isinstance(target, BankRefundIntent)
        else mapper.columns.keys()
    )
    if any(state.attrs[name].history.has_changes() for name in fields):
        raise BankRefundFactImmutableError("bank_refund_identity_is_immutable")


def _guard_immutable_delete(_mapper, _connection, _target):
    raise BankRefundFactImmutableError("bank_refund_fact_deletion_forbidden")


for _model in (OriginalBankPaymentFact, BankRefundIntent, PaymentCashEvent):
    event.listen(_model, "before_update", _guard_immutable_update)
    event.listen(_model, "before_delete", _guard_immutable_delete)
