"""Reservation policy and the single audited bank refund writer.

Installed only with the explicit migrated-schema flag. Bank capabilities are
injected after their actual contract is verified; no config/OAuth inference and
no startup bank transaction exists. Manager explanations stay in local audit.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import refund_schema
from .miniapp_security import AccessError
from .models import BankPayment, Booking, Receipt
from .timeutil import as_utc as _utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StaleCancellationNotice:
    repeated: bool
    stale: bool = True


def _kopecks(value):
    value = Decimal(str(value)) * 100
    if not value.is_finite() or value != value.to_integral_value() or not 0 <= value <= 99_999_999_999_999:
        raise ValueError('verified_money_invalid')
    return int(value)


def build_booking_refunds(engine, *, adapter=None, clock=None):
    if not refund_schema.install():
        return None
    return BookingRefundIntegration(engine, adapter=adapter, clock=clock)


async def bind_new_receipt(session, receipt, booking):
    if not refund_schema.enabled():
        return
    from sqlalchemy import inspect

    from .booking_reservation_models import BookingReservationVersion
    if not inspect(receipt).pending or receipt not in session.new:
        raise ValueError('new_receipt_binding_required')
    version_id = getattr(booking, 'reservation_version_id', None)
    if version_id is None:
        # An older booking remains explicitly unbound. No invented backfill.
        receipt.reservation_version_id = None
        return
    with session.no_autoflush:
        version = await session.get(BookingReservationVersion, version_id)
    if version is None or version.booking_id != booking.id:
        raise ValueError('active_reservation_binding_invalid')
    receipt.reservation_version_id = version.id


class BookingRefundIntegration:
    def __init__(self, engine, *, adapter=None, clock=None):
        from .bank_refunds import BankRefunds
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(UTC))
        self.service = BankRefunds(hooks=self, adapter=adapter, clock=self.clock)

    async def read_version(self, session, booking):
        value = booking.payment_money_version
        if type(value) is not int or value < 0:
            raise ValueError('payment_money_version_invalid')
        return value

    async def bump_version(self, session, booking):
        booking.payment_money_version = await self.read_version(session, booking) + 1
        await session.flush()
        return booking.payment_money_version

    async def bind_bill(self, session, bill, booking):
        from sqlalchemy import inspect

        from .bank_refunds import RefundError
        from .booking_reservation_models import BookingReservationVersion
        if not inspect(bill).pending or bill not in session.new:
            raise RefundError('new_bank_bill_binding_required')
        with session.no_autoflush:
            version = await session.get(BookingReservationVersion, booking.reservation_version_id) if booking.reservation_version_id else None
        if version is None or version.booking_id != booking.id or booking.status in {'CANCELLED', 'REJECTED'}:
            raise RefundError('refund_original_reservation_unavailable')
        bill.reservation_version_id = version.id

    async def bind_receipt_from_bill(self, session, bill, receipt, *, created):
        from .bank_refunds import RefundError
        from .booking_reservation_models import BookingReservationVersion
        if receipt.booking_id != bill.booking_id or not bill.reservation_version_id:
            raise RefundError('refund_original_reservation_unavailable')
        with session.no_autoflush:
            version = await session.get(BookingReservationVersion, bill.reservation_version_id)
        if version is None or version.booking_id != bill.booking_id:
            raise RefundError('refund_original_reservation_unavailable')
        if created is True:
            if receipt.reservation_version_id not in (None, version.id):
                raise RefundError('refund_original_reservation_unavailable')
            receipt.reservation_version_id = version.id
        elif receipt.reservation_version_id != version.id:
            raise RefundError('refund_original_reservation_unavailable')

    def require_working_order_access(self):
        from .bank_refunds import RefundError
        adapter = self.service.adapter
        if adapter is None:
            raise RefundError('refund_working_order_access_unconfirmed')
        adapter.capabilities.require_order_evidence()
        if adapter.client.config.environment != 'production':
            raise RefundError('refund_production_original_required')
        return adapter

    def verify_order_observation(self, bill, raw_order_details):
        from .bank_refunds import OrderIdentity, RefundError, _metadata
        adapter = self.require_working_order_access()
        meta = _metadata(bill)
        identity = OrderIdentity(bill.id, bill.bank_order_id, bill.order_number,
                                 _kopecks(bill.amount), meta['binding'])
        if adapter._binding() != identity.merchant_binding_hash:
            raise RefundError('refund_original_bank_binding_unavailable')
        return adapter._verify_order(identity, raw_order_details)

    async def bank_history_present(self, session, bill):
        from .bank_refund_models import OriginalBankPaymentFact
        return bool(await session.scalar(select(OriginalBankPaymentFact.id).where(
            OriginalBankPaymentFact.bank_payment_id == bill.id).limit(1)))

    async def apply_verified_bank_evidence(self, session, booking, bill, evidence):
        return await self.service.apply_verified_bank_evidence(session,
            booking_id=booking.id, bill_id=bill.id, evidence=evidence,
            expected_money_version=await self.read_version(session, booking))

    async def manual_approved(self, session, booking, receipt):
        identity = 'owner-review:' + hashlib.sha256(json.dumps([
            receipt.id, receipt.reviewed_by_id, str(receipt.reviewed_at),
            str(receipt.verified_amount), receipt.approved_image_hash,
            receipt.approved_operation_key], separators=(',', ':')).encode()).hexdigest()
        if receipt.reservation_version_id is None:
            # Existing unbound history is retained for reconciliation, never
            # converted into a made-up bank origin or a new reservation.
            await self.bump_version(session, booking)
            await self.after_money_change(session, booking.id, None)
            return None
        return await self.service.record_manual_receipt_cash_event(session,
            booking_id=booking.id, receipt_id=receipt.id, approval_identity=identity,
            expected_money_version=await self.read_version(session, booking))

    async def document_admitted(self, session, booking, receipt):
        """Invalidate stale refund plans without treating OCR as bank cash.

        The automatic receipt policy owns idempotency and calls this only on
        its PENDING -> APPROVED transition, with the booking lock held. A late
        document must reassess existing cancellation cases even though it has
        no verified bank origin and therefore cannot create a cash event.
        """
        from .services.automatic_receipts import KEY, analysis_of
        state = analysis_of(receipt).get(KEY)
        if (receipt.booking_id != booking.id or receipt.status != 'APPROVED'
                or receipt.reviewed_by_id is not None or not isinstance(state, dict)
                or state.get('state') != 'accepted'):
            raise ValueError('automatic_receipt_admission_required')
        await self.bump_version(session, booking)
        await self.after_money_change(session, booking.id, None)

    async def cancel_booking(self, session, booking, actor_id, *, revision, current_revision,
                             reason, force_majeure, received_at, shooting, has_sale, mode,
                             requested_version_id=None, requested_version_present=False):
        from .booking_reservation_models import (
            BookingCancellationCase,
            BookingReservationVersion,
        )
        from .models import AuditLog, DeliveryGallery
        from .services.booking_reservations import (
            ReservationConflict,
            _uuid,
            authorize_change,
            record_first_notice,
        )
        actor = await authorize_change(session, booking, actor_id)
        requested = _uuid(requested_version_id) if requested_version_id is not None else None
        if requested is not None:
            slot = await session.get(BookingReservationVersion, requested)
            if slot is None or slot.booking_id != booking.id:
                raise ReservationConflict('requested_slot_not_available')
        key = 'bc-' + hashlib.sha256(json.dumps([actor_id, booking.id, revision,
            reason.strip(), force_majeure, requested_version_present, requested],
            separators=(',', ':')).encode()).hexdigest()[:40]
        previous = await session.scalar(select(BookingCancellationCase).where(
            BookingCancellationCase.first_actor_id == actor_id,
            BookingCancellationCase.request_key == key))
        if previous is None and (current_revision != revision or
                requested_version_present and requested != booking.reservation_version_id):
            # A rejected stale card is audit evidence, not an authoritative
            # cancellation of any reservation. It consumes no case/key and
            # cannot freeze credit or block a later legitimate cancellation.
            old = list(await session.scalars(select(AuditLog.details).where(
                AuditLog.entity=='booking', AuditLog.entity_id==booking.id,
                AuditLog.action=='booking_cancellation_stale_card')))
            def matches(value):
                try:
                    decoded=json.loads(value or '{}')
                    return isinstance(decoded,dict) and decoded.get('requestKey')==key
                except (ValueError,TypeError):
                    return False
            repeated = any(matches(value) for value in old)
            if not repeated:
                from .services.core import audit
                await audit(session, actor, 'booking_cancellation_stale_card', 'booking', booking.id,
                    json.dumps({'requestKey':key,'receivedAtUTC':_utc(received_at).isoformat(),
                        'requestedVersionId':requested,'currentVersionId':booking.reservation_version_id,
                        'reason':reason.strip(),'forceMajeure':force_majeure},ensure_ascii=False))
            return StaleCancellationNotice(repeated)
        if previous is None and booking.reservation_version_id is None and booking.status in {'CANCELLED', 'REJECTED'}:
            raise ReservationConflict('legacy_cancellation_notice_requires_review')
        await self.service.lock_context(session, booking.id)
        gallery = await session.scalar(select(DeliveryGallery).where(
            DeliveryGallery.booking_id == booking.id))
        complete = bool(shooting and (shooting.completed_at or shooting.status in {
            'SHOT', 'READY_FOR_MANAGER', 'READY_FOR_SALE', 'COMPLETED', 'SOLD'}))
        started = bool(shooting and (shooting.started_at or shooting.status in {
            'SHOOTING', 'SHOT', 'READY_FOR_MANAGER', 'READY_FOR_SALE', 'COMPLETED', 'SOLD'}))
        delivered = bool(gallery and (gallery.published or gallery.handed_at or gallery.downloaded_at))
        result = await record_first_notice(session, booking, actor_id,
            active_version_id=booking.reservation_version_id,
            requested_version_id=previous.reservation_version_id if previous else
                requested_version_id if requested_version_present else booking.reservation_version_id,
            request_key=key, reason=reason, force_majeure=force_majeure,
            received_at=_utc(received_at), recorded_at=self.clock(), has_sales=has_sale,
            work_started=started, work_completed=complete, has_delivery=delivered)
        if result.repeated:
            return result
        if shooting is not None:
            shooting.status = 'CANCELLED'
        await self.bump_version(session, booking)
        await self.assess_case(session, result.case)
        # Creating the durable plan is safe after the explicit bank contract;
        # submission is performed after commit by the background loop.
        if self.service.adapter is not None:
            from .bank_refunds import RefundError
            try:
                await self.prepare_case(session, result.case)
            except RefundError as error:
                if str(error) not in {'refund_capability_unconfirmed',
                    'refund_working_order_access_unconfirmed', 'refund_correlation_contract_missing'}:
                    raise
        return result

    async def after_money_change(self, session, booking_id, reservation_key):
        from .booking_reservation_models import BookingCancellationCase
        cases = list(await session.scalars(select(BookingCancellationCase).where(
            BookingCancellationCase.booking_id == booking_id).order_by(
            BookingCancellationCase.id).with_for_update()))
        for case in cases:
            if reservation_key is None or case.reservation_key == reservation_key:
                await self.assess_case(session, case)
        from .services.receipts import refresh_payment_statuses
        await refresh_payment_statuses(session, booking_id)

    async def assess_case(self, session, case):
        from dataclasses import asdict

        from .bank_refund_models import BankRefundIntent
        from .bank_refunds import RefundError
        from .booking_reservation_models import BookingReservationVersion
        from .services.booking_deposit_policy import DepositReceipt
        from .services.booking_reservations import (
            BoundPolicyReceipt,
            ReservationConflict,
            apply_business_assessment,
            cancellation_assessment,
        )
        if not case.reservation_key or case.state in {'STALE_SLOT_REVIEW', 'LEGACY_FACTS_UNAVAILABLE'}:
            return
        versions = list(await session.scalars(select(BookingReservationVersion).where(
            BookingReservationVersion.booking_id == case.booking_id)))
        slot = next((v for v in versions if v.id == case.reservation_version_id), None)
        intents = list(await session.scalars(select(BankRefundIntent).where(BankRefundIntent.case_id == case.id)))
        try:
            originals = {o.receipt_id: o for o in await self.service.load_original_payments_for_policy(
                session, booking_id=case.booking_id, reservation_key=case.reservation_key)}
            receipts = list(await session.scalars(select(Receipt).where(Receipt.booking_id == case.booking_id)))
            facts = tuple(BoundPolicyReceipt(case.booking_id, r.reservation_version_id,
                DepositReceipt(r.id, r.purpose, r.status,
                    _kopecks(r.verified_amount) if r.status == 'APPROVED' else None,
                    originals.get(r.id))) for r in receipts)
            assessment = cancellation_assessment(slot, case, facts, versions=versions,
                                                 checked_at=self.clock())
            if not intents:
                apply_business_assessment(case, assessment, checked_at=self.clock())
                return
            decision = assessment.decision
            confirmed = sum(i.confirmed_kopecks for i in intents)
            # Case projections retain the lifetime authorised amount. Receipt
            # NET is current money; confirmed negative events remain dated.
            # This permits a separately verified late PAY to get its own one-
            # shot intent without changing or resubmitting the earlier request.
            case.verified_net_kopecks = decision.verified_net_kopecks + confirmed
            case.retained_kopecks = decision.retained_kopecks
            case.eligible_refund_kopecks = decision.eligible_refund_kopecks + confirmed
            value = asdict(decision)
            for name in ('notice_moscow_date', 'reserved_date'):
                value[name] = str(value[name])
            value['refunds'] = list(value['refunds'])
            value['confirmed_refund_kopecks'] = confirmed
            if case.decision != value:
                case.decision = value
                case.decision_revision += 1
                case.assessed_at = self.clock()
            if decision.disposition.startswith('BLOCKED_'):
                case.state = decision.disposition
                return
            states = {i.state for i in intents}
            requested = sum(i.requested_kopecks for i in intents)
            if confirmed == requested == case.eligible_refund_kopecks and states == {'CONFIRMED'}:
                case.state = 'REFUNDED'
            elif states & {'UNKNOWN', 'SUBMITTING', 'RECONCILIATION_REQUIRED', 'FAILED'}:
                case.state = 'REFUND_UNKNOWN'
            elif states & {'PENDING', 'CONFIRMED'}:
                case.state = 'REFUND_PENDING'
            else:
                case.state = 'REFUND_READY'
        except (RefundError, ReservationConflict):
            case.state = 'BLOCKED_PAYMENT_RECONCILIATION'

    async def photo_credit(self, session, booking):
        from .bank_refund_models import OriginalBankPaymentFact
        from .booking_reservation_models import (
            BookingCancellationCase,
            BookingReservationVersion,
        )
        from .services.booking_reservations import (
            ReceiptCreditFact,
            resolve_photo_credit,
        )
        versions = list(await session.scalars(select(BookingReservationVersion).where(
            BookingReservationVersion.booking_id == booking.id)))
        cases = list(await session.scalars(select(BookingCancellationCase).where(
            BookingCancellationCase.booking_id == booking.id)))
        receipts = list(await session.scalars(select(Receipt).where(Receipt.booking_id == booking.id)))
        bills = list(await session.scalars(select(BankPayment).where(BankPayment.booking_id == booking.id)))
        held = set()
        for bill in bills:
            if bill.receipt_id is not None and (bill.status == 'RECONCILIATION_REQUIRED' or
                    not await session.scalar(select(OriginalBankPaymentFact.id).where(
                        OriginalBankPaymentFact.bank_payment_id == bill.id).limit(1))):
                held.add(bill.receipt_id)
        active = next((v for v in versions if v.id == booking.reservation_version_id), None)
        if active is not None:
            snapshot = await self.service.money_snapshot(session, booking_id=booking.id,
                                                        reservation_key=active.reservation_key)
            if snapshot.reconciliation_required:
                # A nonempty fact is not sufficient proof of today's NET.
                # Validate immutable history before allowing any of this
                # reservation's mutable receipts to pay for photographs.
                active_ids = {v.id for v in versions if v.reservation_key == active.reservation_key}
                held.update(r.id for r in receipts if r.reservation_version_id in active_ids)
        return resolve_photo_credit(booking.id, booking.reservation_version_id, versions, cases,
            tuple(ReceiptCreditFact(r.id, r.booking_id, r.reservation_version_id, r.purpose,
                r.status, _kopecks(r.verified_amount) if r.status == 'APPROVED' else None)
                for r in receipts), reconciliation_held_receipt_ids=frozenset(held))

    async def prepare_case(self, session, case):
        from .bank_refund_models import BankRefundIntent, OriginalBankPaymentFact
        from .bank_refunds import RefundError
        from .models import Booking
        await self.assess_case(session, case)
        if case.state not in {'REFUND_READY', 'REFUND_PENDING', 'REFUND_UNKNOWN'}:
            return ()
        # Never reserve/dispatch without separately confirmed bank capabilities.
        if self.service.adapter is None:
            return ()
        self.service.adapter.require_submission()
        facts = list(await session.scalars(select(OriginalBankPaymentFact).where(
            OriginalBankPaymentFact.booking_id == case.booking_id,
            OriginalBankPaymentFact.reservation_key == case.reservation_key)))
        existing = list(await session.scalars(select(BankRefundIntent).where(BankRefundIntent.case_id == case.id)))
        # The policy authorizes the whole net deposit. Fact-level allocation is
        # bounded by each original PAY, after already confirmed refunds.
        from .bank_refund_models import PaymentCashEvent
        amounts = {str(i.original_pay_fact_id): i.requested_kopecks for i in existing}
        for fact in facts:
            if str(fact.id) in amounts:
                continue
            if await session.scalar(select(Receipt.purpose).where(Receipt.id == fact.receipt_id)) != 'DEPOSIT':
                continue
            refunded = -sum((await session.scalars(select(PaymentCashEvent.amount_kopecks).where(
                PaymentCashEvent.original_pay_fact_id == fact.id,
                PaymentCashEvent.origin_type == 'BANK_REFUND'))).all())
            amount = fact.amount_kopecks - refunded
            if amount:
                amounts[fact.id] = amount
        booking = await session.get(Booking, case.booking_id)
        if sum(amounts.values()) != case.eligible_refund_kopecks:
            raise RefundError('refund_case_amount_mismatch')
        return await self.service.prepare_refund_intents(session, booking_id=case.booking_id,
            case_id=case.id, request_key='c-' + str(case.id).replace('-', ''),
            expected_money_version=await self.read_version(session, booking), amounts=amounts)

    async def public_status(self, session, booking):
        from .bank_refund_models import BankRefundIntent
        from .booking_reservation_models import BookingCancellationCase
        cases = list(await session.scalars(select(BookingCancellationCase).where(
            BookingCancellationCase.booking_id == booking.id).order_by(BookingCancellationCase.received_at)))
        items = []
        for case in cases:
            intents = list(await session.scalars(select(BankRefundIntent).where(BankRefundIntent.case_id == case.id)))
            items.append({'id': case.id, 'state': case.state,
                'noticeAt': _utc(case.received_at).isoformat(), 'reason': case.reason,
                'kind': case.request_kind, 'category': (case.decision or {}).get('category'),
                'eligibleAmount': str(Decimal(case.eligible_refund_kopecks) / 100),
                'retainedAmount': str(Decimal(case.retained_kopecks) / 100),
                'confirmedRefundAmount': str(Decimal(sum(i.confirmed_kopecks for i in intents)) / 100),
                'bankConfirmed': bool(intents) and all(i.state == 'CONFIRMED' for i in intents)})
        capable = False
        try:
            if self.service.adapter is not None:
                self.service.adapter.require_submission(); capable = True
        except ValueError:
            pass
        return {'bookingId': booking.id, 'moneyVersion': await self.read_version(session, booking),
            'automaticRefundCapability': capable, 'cases': items,
            'legacyBindingRequiresReview': booking.reservation_version_id is None}

    async def process_once(self, *, limit=10):
        from .bank_refund_models import BankRefundIntent
        from .bank_refunds import RefundError
        adapter = self.service.adapter
        if adapter is None:
            return 0
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        from .booking_reservation_models import BookingCancellationCase
        async with sessions() as session:
            plans = list((await session.execute(select(BookingCancellationCase.id,
                BookingCancellationCase.booking_id).where(BookingCancellationCase.state.in_(
                ['REFUND_READY', 'REFUND_PENDING', 'REFUND_UNKNOWN'])).order_by(
                BookingCancellationCase.received_at).limit(limit))).all())
        for cid, bid in plans:
            try:
                adapter.require_submission()
                async with sessions() as session, session.begin():
                    await self.service.lock_context(session, bid, case_id=cid)
                    case = await session.get(BookingCancellationCase, cid)
                    await self.prepare_case(session, case)
            except RefundError:
                continue
        async with sessions() as session:
            rows = list((await session.execute(select(BankRefundIntent.id, BankRefundIntent.booking_id,
                BankRefundIntent.state).where(BankRefundIntent.state.in_(
                    ['READY','SUBMITTING','UNKNOWN','PENDING','RECONCILIATION_REQUIRED']))
                .order_by(BankRefundIntent.created_at).limit(limit))).all())
        processed = 0
        for iid, bid, state in rows:
            try:
                if state == 'READY':
                    adapter.require_submission()
                    async with sessions() as session:
                        booking = await session.get(Booking, bid)
                        expected = await self.read_version(session, booking)
                    await self.service.submit_refund(sessions, booking_id=bid,
                        intent_id=iid, expected_money_version=expected)
                else:
                    adapter.require_reconciliation()
                    await self.service.reconcile_refund(sessions, booking_id=bid, intent_id=iid)
                processed += 1
            except RefundError:
                # A version race leaves READY untouched. An unknown outcome
                # retains its original intent and can only be read/reconciled.
                continue
        return processed


async def refund_loop(integration, *, interval=30):
    failures = 0
    while True:
        try:
            await integration.process_once()
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one-shot claims; never log bank or guest data
            # Do not log DB parameter values, bank responses or guest reasons.
            # Each durable claim remains one-shot; another cycle only reads
            # SUBMITTING/UNKNOWN and never turns it into a new submission.
            failures = min(failures + 1, 4)
            logger.warning('Booking refund cycle failed; durable intents retained')
        await asyncio.sleep(min(interval * (2 ** failures), 300))


def install_booking_refunds(app, api, *, adapter=None):
    service = build_booking_refunds(api.engine, adapter=adapter)
    api.booking_refunds = service
    if service is None:
        return None
    app['booking_refunds'] = service

    async def status(request):
        from .people import positive_id
        from .services.booking_reservations import ReservationConflict, authorize_change
        async with AsyncSession(api.engine) as session:
            booking = await session.get(Booking, positive_id(request.match_info['id']))
            try:
                if booking is None:
                    raise ReservationConflict('booking_cancellation_forbidden')
                await authorize_change(session, booking, request['miniapp_actor']['id'])
            except ReservationConflict:
                raise AccessError('Возврат по этой записи недоступен.', 403) from None
            return web.json_response(await service.public_status(session, booking))

    app.router.add_get('/api/miniapp/bookings/{id}/refund', status)
    if adapter is not None:
        async def lifecycle(_app):
            task = asyncio.create_task(refund_loop(service), name='booking_refunds')
            try:
                yield
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        app.cleanup_ctx.append(lifecycle)
    return service
