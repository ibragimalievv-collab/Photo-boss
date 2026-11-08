"""Automatic receipt evidence on the existing, single payment ledger.

No human approval is part of this path. APPROVED here admits a document for the
operational workflow; only an independently reconciled BankPayment proves bank
settlement. Callers lock the booking before the receipt. Provider I/O belongs
outside both the transaction and the database pool.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import uuid

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import BankPayment, Booking, Receipt, utc_now
from .core import audit
from .receipt_policy import MESSAGES, RecipientProfile, VERSION, evaluate, operation_identity
from .receipts import expected_amount, money, refresh_payment_statuses

KEY = 'autoReceipt'
MAX_ATTEMPTS = 3
LEASE_SECONDS = 180
RETRY_SECONDS = (5, 20)
MESSAGES.update({
    'duplicate_payment': 'Этот чек или номер операции уже учтён. Повторной оплаты не добавлено.',
    'amount_changed': 'Сумма к оплате изменилась. Обновите запись и приложите чек на текущую доплату.',
    'file_missing': 'Файл чека не сохранён полностью. Прикрепите его повторно.',
    'bank_order_unlinked': 'Чек не удалось связать с открытым банковским счётом. Автоматическая сверка банка продолжается; повторной оплаты не добавлено.',
    'retry_exhausted': 'Сервис проверки пока недоступен. Чек сохранён. Повторную AI-проверку можно запустить кнопкой ниже.',
    'archived_period': 'Чек относится к архивному рабочему периоду. История сохранена; в новые оплаты он не зачислен.',
})


def enabled():
    return os.getenv('PHOTO_BOSS_RECEIPT_AUTO', '0') == '1'


def recipient_profile():
    try:
        return RecipientProfile.parse(json.loads(os.getenv('PHOTO_BOSS_RECEIPT_RECIPIENT_JSON', 'null')))
    except (ValueError, TypeError):
        return None


def analysis_of(row):
    try:
        value = json.loads(row.analysis or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def public_state(row):
    state = analysis_of(row).get(KEY)
    if not isinstance(state, dict):
        return None
    codes = [c for c in state.get('codes', []) if c in MESSAGES]
    return {'state': state.get('state'), 'attempts': state.get('attempts', 0),
            'messages': [MESSAGES[c] for c in codes], 'policyVersion': state.get('policyVersion'),
            'creditedAmount': state.get('creditedAmount'),
            'evidenceType': 'AI_RECEIPT', 'bankConfirmed': bool(analysis_of(row).get('bankSettlement'))}


def _time(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _save(row, analysis, state, *, now):
    previous = analysis.get(KEY, {})
    if isinstance(previous, dict) and 'retryRequestedAt' in previous:
        state = {'retryRequestedAt': previous['retryRequestedAt'], **state}
    state = dict(state, policyVersion=VERSION, updatedAt=_time(now).isoformat())
    analysis[KEY] = state
    row.analysis = json.dumps(analysis, ensure_ascii=False, allow_nan=False)


async def _identity_locks(session, row, operation):
    # A transfer submitted to two different bookings must serialize as well.
    # Unique approved claims below remain the final database backstop.
    if session.get_bind().dialect.name == 'postgresql':
        operations = operation if isinstance(operation, (list, tuple)) else [operation]
        keys = ({row.image_sha256, *operations, *_document_hashes(row)} - {None, ''})
        for value in sorted(keys):
            key = int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], 'big', signed=True)
            await session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


def _document_hashes(row):
    docs = analysis_of(row).get('documents', [])
    return {doc['sha256'] for doc in docs if isinstance(doc, dict)
            and re.fullmatch(r'[a-f0-9]{64}', str(doc.get('sha256', '')))} | {row.image_sha256}


def _credited_payments(row):
    value = analysis_of(row)
    state = value.get(KEY, {})
    saved = state.get('creditedPayments', []) if isinstance(state, dict) else []
    if saved and all(isinstance(item, dict) and isinstance(item.get('key'), str)
                     and isinstance(item.get('amount'), str) for item in saved):
        return {item['key']: item['amount'] for item in saved}
    # Read historical single-receipt evidence without rewriting its decisions.
    key = operation_identity(value.get('fields', {}))
    keys = {key, row.operation_key, row.approved_operation_key} - {None, ''}
    return {key: str(money(row.verified_amount)) for key in keys} if row.verified_amount is not None else {}


async def admit(session, booking, row, *, profile=None, now=None, attempts=None):
    """Admit once under the caller's booking lock; never call a bank/provider."""
    if not enabled() or row.status != 'PENDING':
        return public_state(row)
    now = _time(now or datetime.now(UTC))
    analysis = analysis_of(row)
    prior = analysis.get(KEY, {})
    prior = prior if isinstance(prior, dict) else {}
    from ..operational_start import OperationalStartError, require_booking_date
    try:
        require_booking_date(booking.shoot_date)
    except OperationalStartError:
        _save(row, analysis, {'state': 'archived', 'codes': ['archived_period'], 'attempts': 0}, now=now)
        return public_state(row)
    count = attempts if attempts is not None else prior.get('attempts', 1)
    profile = profile or recipient_profile()
    items = analysis.get('payments', [analysis.get('fields', {})])
    if not isinstance(items, list) or not 1 <= len(items) <= 2 or any(not isinstance(item, dict) for item in items):
        items = [{}]
        analysis = dict(analysis, status='unavailable')
    decisions = [evaluate(dict(analysis, fields=item), expected=item.get('amount') or 0,
                         profile=profile, created_at=row.created_at or now, now=now) for item in items]
    codes = list(dict.fromkeys(code for decision in decisions for code in decision.codes))
    state = {'state': 'checking', 'codes': codes, 'attempts': count}
    if profile is None:
        state.update(state='waiting_configuration', attempts=0)
    elif 'analysis_unavailable' in codes:
        if count >= MAX_ATTEMPTS:
            state.update(state='retry_exhausted', codes=['retry_exhausted'])
        else:
            state.update(state='retry_scheduled', nextAt=(now + timedelta(
                seconds=RETRY_SECONDS[max(0, count - 1)])).isoformat())
    elif codes:
        state['state'] = 'rejected'
    else:
        payments, orders = {}, {}
        inconsistent = False
        for item, decision in zip(items, decisions):
            key = decision.operation_key
            if key in payments and payments[key] != decision.amount:
                inconsistent = True
            payments[key] = decision.amount
            orders[key] = item.get('order_number')
        await _identity_locks(session, row, list(payments))
        previous = list(await session.scalars(select(Receipt).where(Receipt.id != row.id, Receipt.status == 'APPROVED')))
        other_order, duplicate_group = False, False
        document_hashes = _document_hashes(row)
        new_payments = dict(payments)
        for old in previous:
            prior_payments = _credited_payments(old)
            overlap = payments.keys() & prior_payments.keys()
            document_overlap = bool((document_hashes - {None}) & (_document_hashes(old) - {None}))
            if (overlap or document_overlap) and old.booking_id != row.booking_id:
                other_order = True
            if old.image_sha256 == row.image_sha256:
                duplicate_group = True
            for key in overlap:
                if money(prior_payments[key]) != payments[key]:
                    inconsistent = True
                new_payments.pop(key, None)
        amount = sum(new_payments.values(), Decimal(0))
        outstanding = money(await expected_amount(session, booking, row.purpose))
        bills = list(await session.scalars(select(BankPayment).where(
            BankPayment.booking_id == booking.id,
            BankPayment.status.in_(['CREATING', 'PENDING', 'PAID', 'PARTIAL_REFUND',
                                   'REFUNDED', 'RECONCILIATION_REQUIRED'])).with_for_update()))
        order_values = {orders[key] for key in new_payments}
        order = next(iter(order_values)) if len(order_values) == 1 else None
        matching = [bill for bill in bills if isinstance(order, str) and order == bill.order_number
                    and money(bill.amount) == amount and bill.receipt_id in (None, row.id)
                    and bill.status in {'CREATING', 'PENDING'} and not bill.test_mode]
        code = ('file_missing' if not row.file_id or not re.fullmatch(r'[a-f0-9]{64}', row.image_sha256 or '') else
                'ambiguous_document' if inconsistent else
                'duplicate_payment' if other_order or duplicate_group or not new_payments else
                'amount_mismatch' if amount > outstanding or amount > money(row.expected_amount) else
                'amount_changed' if outstanding <= 0 else
                'bank_order_unlinked' if bills and len(matching) != 1 else None)
        if code:
            state.update(state='rejected', codes=[code])
        else:
            # A conflict is local to this savepoint; keep the uploaded evidence
            # and return a useful rejection rather than rolling back the order.
            try:
                async with session.begin_nested():
                    operation = (next(iter(new_payments)) if len(new_payments) == 1 else
                                 hashlib.sha256(('payments:' + ':'.join(sorted(new_payments))).encode()).hexdigest())
                    row.status, row.verified_amount = 'APPROVED', amount
                    row.operation_key = row.approved_operation_key = operation
                    row.approved_image_hash = row.image_sha256
                    row.reviewed_by_id, row.reviewed_at = None, utc_now()
                    await session.flush()
            except IntegrityError:
                await session.refresh(row)
                state.update(state='rejected', codes=['duplicate_payment'])
            else:
                state.update(state='accepted', codes=[], creditedAmount=str(money(amount)),
                             creditedPayments=[{'key': key, 'amount': str(money(value))}
                                               for key, value in sorted(new_payments.items())])
                if matching:
                    # Exact issued-order binding only. Do not set PAID: a later
                    # genuine bank status will update this same receipt row.
                    matching[0].receipt_id = row.id
                await refresh_payment_statuses(session, booking.id)
    if state['state'] == 'rejected':
        row.status, row.verified_amount = 'REJECTED', None
        row.reviewed_by_id, row.reviewed_at = None, utc_now()
    _save(row, analysis, state, now=now)
    if state['state'] == 'accepted':
        from ..booking_refund_integration import build_booking_refunds
        integration = build_booking_refunds(session.bind)
        if integration is not None:
            await integration.document_admitted(session, booking, row)
    if state['state'] in {'accepted', 'rejected'}:
        await audit(session, None, 'receipt_auto_' + state['state'], 'receipt', row.id,
                    json.dumps({'policy': VERSION, 'codes': state['codes'], 'bankConfirmed': False}))
    await session.flush()
    return public_state(row)


async def existing_upload(session, booking_id, purpose, digest):
    """A second request key/file replay reuses evidence, never creates money."""
    row = await session.scalar(select(Receipt).where(Receipt.image_sha256 == digest).order_by(Receipt.id).limit(1))
    if row and (row.booking_id != booking_id or row.purpose != purpose):
        raise ValueError(MESSAGES['duplicate_payment'])
    return row


async def pending_upload(session, booking_id, purpose):
    row = await session.scalar(select(Receipt).where(Receipt.booking_id == booking_id,
        Receipt.purpose == purpose, Receipt.status == 'PENDING').order_by(Receipt.id).limit(1))
    if row and enabled():
        state = analysis_of(row).get(KEY, {})
        if isinstance(state, dict) and state.get('state') == 'retry_exhausted':
            return None  # Replacement may supersede this uncredited document.
    return row


async def supersede_exhausted(session, booking_id, purpose, replacement_id):
    rows = await session.scalars(select(Receipt).where(Receipt.booking_id == booking_id,
        Receipt.purpose == purpose, Receipt.id != replacement_id, Receipt.status == 'PENDING'))
    for row in rows:
        analysis = analysis_of(row)
        state = analysis.get(KEY, {})
        if isinstance(state, dict) and state.get('state') == 'retry_exhausted':
            row.status = 'REJECTED'
            _save(row, analysis, dict(state, state='superseded', replacementId=replacement_id), now=utc_now())


async def claim(engine, *, now=None):
    if not enabled() or recipient_profile() is None:
        return None
    now = _time(now or datetime.now(UTC))
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        rows = list(await session.scalars(select(Receipt).where(Receipt.status == 'PENDING')
                                         .order_by(Receipt.id)))
        for preview in rows:
            state = analysis_of(preview).get(KEY, {})
            state = state if isinstance(state, dict) else {}
            if isinstance(state, dict) and state.get('state') in {'retry_exhausted', 'rejected', 'accepted', 'superseded', 'archived'}:
                continue
            try:
                due = _time(datetime.fromisoformat(state.get('nextAt', '1970-01-01T00:00:00+00:00')))
            except (ValueError, TypeError):
                continue
            if due > now:
                continue
            booking = await session.scalar(select(Booking).where(Booking.id == preview.booking_id)
                                           .with_for_update(skip_locked=True))
            if booking is None:
                continue
            row = await session.scalar(select(Receipt).where(Receipt.id == preview.id)
                                       .with_for_update().execution_options(populate_existing=True))
            if row.status != 'PENDING':
                continue
            analysis = analysis_of(row)
            state = analysis.get(KEY, {})
            state = state if isinstance(state, dict) else {}
            # Re-evaluate the durable lease after waiting for another worker's
            # booking lock. The pre-lock preview may already be stale.
            if state.get('state') in {'retry_exhausted', 'rejected', 'accepted', 'superseded', 'archived'}:
                continue
            try:
                if _time(datetime.fromisoformat(state.get('nextAt', '1970-01-01T00:00:00+00:00'))) > now:
                    continue
            except (ValueError, TypeError):
                continue
            from ..operational_start import OperationalStartError, require_booking_date
            try:
                require_booking_date(booking.shoot_date)
            except OperationalStartError:
                _save(row, analysis, {'state': 'archived', 'attempts': 0, 'codes': ['archived_period']}, now=now)
                continue
            attempts = int(state.get('attempts', 0)) if isinstance(state, dict) else 0
            if attempts >= MAX_ATTEMPTS:
                _save(row, analysis, {'state': 'retry_exhausted', 'attempts': attempts,
                                      'codes': ['retry_exhausted']}, now=now)
                continue
            token = uuid.uuid4().hex
            _save(row, analysis, {'state': 'checking', 'attempts': attempts + 1,
                'claim': token, 'codes': [], 'nextAt': (now + timedelta(seconds=LEASE_SECONDS)).isoformat()}, now=now)
            return {'id': row.id, 'booking': row.booking_id, 'file': row.file_id,
                    'claim': token, 'attempts': attempts + 1}
    return None


async def finish(engine, job, result, *, now=None):
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        booking = await session.scalar(select(Booking).where(Booking.id == job['booking']).with_for_update())
        row = await session.scalar(select(Receipt).where(Receipt.id == job['id']).with_for_update())
        prior = analysis_of(row).get(KEY, {}) if row else {}
        if (not row or row.status != 'PENDING' or not isinstance(prior, dict)
                or prior.get('claim') != job['claim']):
            return False
        prior_analysis = analysis_of(row)
        row.analysis = json.dumps(dict(result, documents=result.get('documents', prior_analysis.get('documents', [])),
                                      **{KEY: prior}), ensure_ascii=False)
        await admit(session, booking, row, attempts=job['attempts'], now=now)
        return True


async def run_once(engine, context, *, extractor=None):
    from .receipts import extract_receipt
    from .media_files import load_private_media
    job = await claim(engine)
    if job is None:
        return False
    try:
        content = await load_private_media(context, job['file'], max_bytes=16 * 1024 * 1024 + 2048)
        result = await (extractor or extract_receipt)(content)
    except asyncio.CancelledError:
        raise  # Durable lease expires; no unbounded immediate retry.
    except Exception:
        result = {'status': 'unavailable'}
    return await finish(engine, job, result)


async def worker(engine, context):
    while True:
        try:
            worked = await run_once(engine, context)
        except asyncio.CancelledError:
            raise
        except Exception:
            worked = False  # Bodies can contain private media references.
        await asyncio.sleep(1 if worked else 5)
