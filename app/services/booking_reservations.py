"""Business facts and a credit read model, with no bank or provider I/O.

Not installed in application handlers yet. Except for a brand-new pending
Booking, callers must hold its canonical row lock. A cancellation caller locks
Booking -> sorted Bills -> sorted Receipts BEFORE this module locks its case;
the sole bank writer subsequently owns case -> sorted RefundIntents.

active_version_id and received_at are server-loaded facts, never trusted HTTP
fields. Receipt version bindings and MoneySnapshot adapters belong to the
coordinated integration. This module never creates an intent or cash event.
"""
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import inspect, select

from ..booking_reservation_models import (
    BookingCancellationCase,
    BookingReservationVersion,
)
from ..models import HotelEmployee, User
from .booking_deposit_policy import (
    MOSCOW,
    POLICY_VERSION,
    RESERVATION_DEPOSIT_KOPECKS,
    CancellationDepositDecision,
    CancellationNotice,
    DepositReceipt,
    ReservedSlotSnapshot,
    evaluate_cancellation_deposit,
)
from .core import roles_of


class ReservationConflict(ValueError):
    """No automatic financial action is safe for these facts."""


def _require(value, code):
    if not value:
        raise ReservationConflict(code)


def _uuid(value):
    try:
        _require(isinstance(value, (str, UUID)), 'invalid_reservation_revision')
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ReservationConflict('invalid_reservation_revision') from None


def _moment(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None
             and value.utcoffset() is not None, 'server_aware_timestamp_required')
    return value.astimezone(UTC)


def _stored_moment(value):
    # SQLite drops timezone offsets from TIMESTAMPTZ emulation. Every write in
    # this module is UTC; accepting naive values here is only for stored facts.
    return _moment(value.replace(tzinfo=UTC) if value.tzinfo is None else value)


def _reason(value):
    _require(isinstance(value, str) and 3 <= len(value.strip()) <= 500,
             'manager_explanation_required')
    return value.strip()


def _slot(day, at):
    _require(type(day) is date and type(at) is time and at.tzinfo is None,
             'moscow_calendar_slot_required')


async def authorize_change(session, booking, actor_id):
    """Current active grants, own booking and actual hotel membership."""
    _require(type(actor_id) is int and actor_id > 0, 'server_actor_required')
    with session.no_autoflush:
        actor = await session.get(User, actor_id)
        roles = await roles_of(session, actor) if actor and actor.active else set()
        allowed = 'OWNER' in roles
        if not allowed and roles & {'ADMIN', 'MANAGER'}:
            member = await session.scalar(select(HotelEmployee.id).where(
                HotelEmployee.user_id == actor_id, HotelEmployee.hotel_id == booking.hotel_id))
            allowed = bool(member and ('ADMIN' in roles or booking.manager_id == actor_id))
    _require(allowed, 'booking_cancellation_forbidden')
    return actor


async def _latest(session, booking_id):
    return await session.scalar(select(BookingReservationVersion).where(
        BookingReservationVersion.booking_id == booking_id).order_by(
        BookingReservationVersion.version.desc()).limit(1))


async def create_initial_version(session, booking, actor_id, *, recorded_at, walk_in=False):
    """Call before the FIRST Booking flush; this cannot backfill a legacy row."""
    _require(inspect(booking).pending and booking in session.new, 'new_unflushed_booking_required')
    _require(type(walk_in) is bool, 'server_walk_in_flag_required')
    if walk_in:
        with session.no_autoflush:
            actor = await session.get(User, actor_id)
            roles = await roles_of(session, actor) if actor and actor.active else set()
            member = await session.scalar(select(HotelEmployee.id).where(
                HotelEmployee.user_id == actor_id, HotelEmployee.hotel_id == booking.hotel_id))
        photographer_self = bool('PHOTOGRAPHER' in roles and member
            and booking.photographer_id == actor_id and booking.manager_id == actor_id)
        if not photographer_self:
            await authorize_change(session, booking, actor_id)
    else:
        await authorize_change(session, booking, actor_id)
    _slot(booking.shoot_date, booking.shoot_time)
    row = BookingReservationVersion(id=str(uuid4()), booking=booking,
        reservation_key=str(uuid4()), version=1, previous_version_id=None,
        shoot_date=booking.shoot_date, shoot_time=booking.shoot_time,
        business_timezone='Europe/Moscow', expected_deposit_kopecks=0 if walk_in else RESERVATION_DEPOSIT_KOPECKS,
        policy_version=POLICY_VERSION, captured_at=_moment(recorded_at),
        captured_by_id=actor_id, provenance='NEW_SERVER', reason='Новая запись без предварительной брони' if walk_in else 'Новая бронь времени')
    session.add(row)
    await session.flush()
    return row


async def append_slot_version(session, booking, actor_id, *, active_version_id,
                              shoot_date, shoot_time, reason, recorded_at, restore=False):
    """Return a new immutable version; caller atomically changes Booking/pointer.

    Ordinary reschedule preserves the reservation key. Cancelled restore is a
    new agreement and new key, with an explicit valid date rather than silently
    moving an old shoot to today. No receipt or bank binding is rewritten.
    """
    await authorize_change(session, booking, actor_id)
    _slot(shoot_date, shoot_time)
    now, explanation = _moment(recorded_at), _reason(reason)
    _require(type(restore) is bool, 'server_restore_flag_required')
    _require(shoot_date >= now.astimezone(MOSCOW).date(), 'new_slot_cannot_be_in_past')
    old = await _latest(session, booking.id)
    _require((old.id if old else None) == (_uuid(active_version_id) if active_version_id else None),
             'active_reservation_revision_changed')
    cancelled = booking.status in {'CANCELLED', 'REJECTED'}
    _require(restore == cancelled, 'restore_requires_cancelled_reservation')
    if old:
        _require(_stored_moment(old.captured_at) <= now, 'reservation_chronology_invalid')
        case = await session.scalar(select(BookingCancellationCase.id).where(
            BookingCancellationCase.booking_id == booking.id,
            BookingCancellationCase.reservation_key == old.reservation_key))
        _require(restore or case is None, 'cancelled_reservation_requires_new_agreement')
    else:
        _require(restore, 'legacy_reservation_facts_unavailable')
    row = BookingReservationVersion(id=str(uuid4()), booking_id=booking.id,
        reservation_key=str(uuid4()) if restore else old.reservation_key,
        version=old.version + 1 if old else 1, previous_version_id=old.id if old else None,
        shoot_date=shoot_date, shoot_time=shoot_time, business_timezone='Europe/Moscow',
        expected_deposit_kopecks=old.expected_deposit_kopecks if old else RESERVATION_DEPOSIT_KOPECKS,
        policy_version=POLICY_VERSION, captured_at=now, captured_by_id=actor_id,
        provenance='NEW_SERVER', reason=explanation)
    session.add(row)
    await session.flush()
    return row


@dataclass(frozen=True)
class NoticeResult:
    case: BookingCancellationCase
    repeated: bool
    needs_review: bool


def _case_needs_review(state):
    return state in {'STALE_SLOT_REVIEW', 'MIXED_MONEY_REVIEW', 'LEGACY_FACTS_UNAVAILABLE',
                     'BLOCKED_ORIGIN_UNAVAILABLE', 'BLOCKED_PAYMENT_RECONCILIATION', 'REFUND_UNKNOWN'}


def _fingerprint(booking_id, requested_version_id, reason, force_majeure):
    raw = json.dumps({'bookingId': booking_id, 'slotRevision': requested_version_id,
        'reason': reason, 'forceMajeure': force_majeure}, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


async def record_first_notice(session, booking, actor_id, *, active_version_id,
        requested_version_id, request_key, reason, force_majeure, received_at,
        recorded_at, has_sales, work_started, work_completed, has_delivery):
    """No commit. Caller commits notice, cancellation and audit atomically.

    A known stale slot is retained as STALE_SLOT_REVIEW without cancelling a
    different current slot. Its server timestamp must survive an HTTP409: caller
    must commit this result rather than raise/roll it back. Exact retries return
    the original case and never cancel a subsequently restored reservation.
    """
    await authorize_change(session, booking, actor_id)
    explanation = _reason(reason)
    _require(type(force_majeure) is bool, 'force_majeure_flag_invalid')
    _require(isinstance(request_key, str) and re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_key),
             'stable_request_key_required')
    received, recorded = _moment(received_at), _moment(recorded_at)
    _require(received <= recorded, 'notice_chronology_invalid')
    requested = _uuid(requested_version_id) if requested_version_id is not None else None
    fingerprint = _fingerprint(booking.id, requested, explanation, force_majeure)
    # Inspect the actor's durable key without locking a case for a DIFFERENT
    # Booking. Only the case under our already-held Booking lock may be locked.
    key_row = (await session.execute(select(BookingCancellationCase.id,
        BookingCancellationCase.booking_id, BookingCancellationCase.request_fingerprint).where(
        BookingCancellationCase.first_actor_id == actor_id,
        BookingCancellationCase.request_key == request_key))).first()
    if key_row:
        case_id, key_booking_id, key_fingerprint = key_row
        _require(key_booking_id == booking.id and key_fingerprint == fingerprint,
                 'request_key_used_for_other_notice')
        by_key = await session.scalar(select(BookingCancellationCase).where(
            BookingCancellationCase.id == case_id).with_for_update().execution_options(populate_existing=True))
        _require(by_key is not None, 'immutable_notice_missing')
        return NoticeResult(by_key, True, _case_needs_review(by_key.state))
    latest = await _latest(session, booking.id)
    _require((latest.id if latest else None) == (_uuid(active_version_id) if active_version_id else None),
             'active_reservation_revision_changed')
    target = await session.get(BookingReservationVersion, requested) if requested else None
    _require((target is not None and target.booking_id == booking.id) if requested else latest is None,
             'requested_slot_not_available')
    if target:
        _require(_stored_moment(target.captured_at) <= received, 'notice_predates_requested_slot')
    query = select(BookingCancellationCase).where(BookingCancellationCase.booking_id == booking.id)
    query = query.where(BookingCancellationCase.reservation_key == target.reservation_key if target else
                        BookingCancellationCase.reservation_key.is_(None))
    existing = await session.scalar(query.with_for_update())
    if existing:
        return NoticeResult(existing, True, _case_needs_review(existing.state))
    flags = (has_sales, work_started, work_completed, has_delivery)
    _require(all(type(value) is bool for value in flags), 'server_work_facts_required')
    _require(not has_sales and not work_completed and not has_delivery,
             'completed_or_sold_work_requires_reconciliation')
    _require(not work_started or force_majeure, 'started_work_requires_force_majeure')
    stale = bool(target and target.id != (latest.id if latest else None))
    state = 'STALE_SLOT_REVIEW' if stale else 'RECEIVED' if target else 'LEGACY_FACTS_UNAVAILABLE'
    case = BookingCancellationCase(id=str(uuid4()), booking_id=booking.id,
        reservation_version_id=target.id if target else None,
        reservation_key=target.reservation_key if target else None, first_actor_id=actor_id,
        request_key=request_key, request_fingerprint=fingerprint, received_at=received,
        recorded_at=recorded, reason=explanation,
        request_kind='FORCE_MAJEURE' if force_majeure else 'ORDINARY',
        policy_version=POLICY_VERSION, state=state, decision_revision=0,
        verified_net_kopecks=0, retained_kopecks=0, eligible_refund_kopecks=0, decision={})
    session.add(case)
    if not stale:
        booking.status, booking.cancellation_reason = 'CANCELLED', explanation
        booking.cancelled_at = received.replace(tzinfo=None)
    await session.flush()
    return NoticeResult(case, False, _case_needs_review(state))


@dataclass(frozen=True)
class ReceiptCreditFact:
    receipt_id: int
    booking_id: int
    reservation_version_id: str | None
    purpose: str
    status: str
    verified_net_kopecks: int | None


@dataclass(frozen=True)
class PhotoCreditResolution:
    available_kopecks: int
    held_kopecks: int
    held_receipt_ids: tuple[int, ...]
    reasons: tuple[str, ...]


def resolve_photo_credit(booking_id, active_version_id, versions, cases, receipts,
                         *, reconciliation_held_receipt_ids=frozenset()):
    """Pure read model, not a second financial ledger or cash-report total.

    Bank-owned reconciliation holds are authoritative additional exclusions.
    Cancelled/stale/unknown original reservations cannot fund a restored one.
    Approved money from any legitimate reschedule version is counted once.
    """
    by_id = {row.id: row for row in versions}
    _require(len(by_id) == len(versions) and all(row.booking_id == booking_id for row in versions),
             'reservation_version_facts_invalid')
    active = by_id.get(active_version_id)
    _require(active_version_id is None or active is not None, 'active_reservation_revision_missing')
    _require(all(row.booking_id == booking_id and
                 ((row.reservation_key is None and row.reservation_version_id is None) or
                  (row.reservation_version_id in by_id and
                   by_id[row.reservation_version_id].reservation_key == row.reservation_key))
                 for row in cases), 'cancellation_case_facts_invalid')
    _require(all(type(value) is int and value > 0 for value in reconciliation_held_receipt_ids),
             'bank_hold_receipt_ids_invalid')
    cancelled_keys = {row.reservation_key for row in cases}
    seen, held_ids, reasons, available, held = set(), [], set(), 0, 0
    for receipt in receipts:
        _require(type(receipt.receipt_id) is int and receipt.receipt_id > 0
                 and receipt.receipt_id not in seen and receipt.booking_id == booking_id
                 and receipt.purpose in {'DEPOSIT', 'PAYMENT'}
                 and receipt.status in {'PENDING', 'APPROVED', 'REJECTED'}, 'receipt_credit_facts_invalid')
        seen.add(receipt.receipt_id)
        if receipt.status != 'APPROVED':
            continue
        amount = receipt.verified_net_kopecks
        _require(type(amount) is int and 0 <= amount <= 99_999_999_999_999, 'verified_net_kopecks_required')
        version = by_id.get(receipt.reservation_version_id)
        why = ('BANK_RECONCILIATION' if receipt.receipt_id in reconciliation_held_receipt_ids else
               'LEGACY_BINDING_UNAVAILABLE' if version is None or active is None else
               'CANCELLED_RESERVATION' if version.reservation_key in cancelled_keys else
               'PREVIOUS_RESERVATION' if version.reservation_key != active.reservation_key else None)
        if why and amount:
            held += amount; held_ids.append(receipt.receipt_id); reasons.add(why)
        elif not why:
            available += amount
    _require(available + held <= 99_999_999_999_999, 'credit_total_exceeds_range')
    _require(set(reconciliation_held_receipt_ids) <= seen, 'bank_hold_receipts_missing')
    return PhotoCreditResolution(available, held, tuple(sorted(held_ids)), tuple(sorted(reasons)))


@dataclass(frozen=True)
class BoundCaseAssessment:
    case_id: str
    slot_revision: str
    decision: CancellationDepositDecision


@dataclass(frozen=True)
class BoundPolicyReceipt:
    booking_id: int
    reservation_version_id: str | None
    receipt: DepositReceipt


def cancellation_assessment(slot, case, receipts, *, versions, checked_at):
    """Only business eligibility, selected by immutable reservation bindings.

    The bank adapter validates original PAY facts; Receipt's creation-version
    binding is supplied separately. A legitimate reschedule keeps the key;
    a restored agreement cannot refund another reservation's money. This cannot
    mark a refund complete or create a bank intent.
    """
    _require(slot is not None and case.reservation_version_id == slot.id
             and case.reservation_key == slot.reservation_key and case.booking_id == slot.booking_id,
             'case_slot_binding_unavailable')
    _require(case.state not in {'STALE_SLOT_REVIEW', 'LEGACY_FACTS_UNAVAILABLE'},
             'case_requires_immutable_facts_review')
    by_id = {version.id: version for version in versions}
    registered = by_id.get(slot.id)
    _require(len(by_id) == len(versions) and all(version.booking_id == case.booking_id
             for version in versions) and registered is not None and
             all(getattr(registered, name) == getattr(slot, name) for name in
                 ('booking_id', 'reservation_key', 'shoot_date', 'shoot_time', 'captured_at')),
             'policy_version_facts_required')
    selected, seen = [], set()
    for fact in receipts:
        _require(isinstance(fact, BoundPolicyReceipt) and fact.booking_id == case.booking_id
                 and isinstance(fact.receipt, DepositReceipt), 'bound_policy_receipts_required')
        receipt = fact.receipt
        _require(receipt.receipt_id not in seen, 'duplicate_policy_receipt')
        seen.add(receipt.receipt_id)
        version = by_id.get(fact.reservation_version_id)
        if receipt.status == 'APPROVED' and receipt.verified_net_kopecks:
            _require(version is not None, 'approved_receipt_binding_unavailable')
        if version is None or version.reservation_key != case.reservation_key:
            continue
        selected.append(receipt)
    _require(not any(r.purpose == 'PAYMENT' and r.status == 'APPROVED' and r.verified_net_kopecks
                     for r in selected), 'mixed_photo_payment_requires_reconciliation')
    decision = evaluate_cancellation_deposit(
        ReservedSlotSnapshot(slot.booking_id, slot.id, slot.shoot_date, slot.shoot_time,
                             _stored_moment(slot.captured_at)),
        CancellationNotice(case.booking_id, slot.id, _stored_moment(case.received_at),
                           case.reason, case.request_kind == 'FORCE_MAJEURE'),
        tuple(selected), checked_at=_moment(checked_at))
    return BoundCaseAssessment(case.id, slot.id, decision)


def apply_business_assessment(case, assessment, *, checked_at):
    """Idempotent local projection. Bank-owned pending/unknown/completed wins."""
    _require(isinstance(assessment, BoundCaseAssessment) and assessment.case_id == case.id
             and assessment.slot_revision == case.reservation_version_id, 'assessment_case_mismatch')
    decision = assessment.decision
    _require(case.state not in {'REFUND_PENDING', 'REFUND_UNKNOWN', 'REFUNDED'},
             'bank_owned_case_state_requires_bank_writer')
    _require(decision.disposition in {'NO_VERIFIED_DEPOSIT', 'RETAINED', 'REFUND_READY',
        'BLOCKED_ORIGIN_UNAVAILABLE', 'BLOCKED_PAYMENT_RECONCILIATION'}, 'business_disposition_required')
    _require(decision.reserved_date is not None and decision.policy_version == case.policy_version,
             'assessment_policy_mismatch')
    _require(decision.reason == case.reason and decision.notice_moscow_date ==
             _stored_moment(case.received_at).astimezone(MOSCOW).date(), 'assessment_notice_mismatch')
    value = asdict(decision)
    for name in ('notice_moscow_date', 'reserved_date'):
        value[name] = str(value[name])
    value['refunds'] = list(value['refunds'])
    if case.decision == value:
        return False
    case.state, case.verified_net_kopecks = decision.disposition, decision.verified_net_kopecks
    case.retained_kopecks, case.eligible_refund_kopecks = decision.retained_kopecks, decision.eligible_refund_kopecks
    case.decision, case.decision_revision = value, case.decision_revision + 1
    case.assessed_at = _moment(checked_at)
    return True
