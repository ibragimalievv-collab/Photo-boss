"""Fail-closed guest SMS scheduling, independent of employee/push reminders.

No vendor is connected. A future adapter must implement idempotent submit and
reconcile by event key, distinguishing accepted/delivered/definitely-not-sent.
Credentials or an arbitrary URL cannot activate the default disabled adapter.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..models import Booking, Client, GuestSMSJob, GuestSMSPreference, Hotel, Setting
from ..operations_guard import operation_lease

log = logging.getLogger(__name__)
CONFIRMED_LIFECYCLE = {'CONFIRMED', 'ASSIGNED', 'PICKED_UP', 'ACCEPTED'}
TERMINAL = {'delivered', 'cancelled', 'rescheduled', 'expired', 'off'}


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def naive(value):
    return utc(value).replace(tzinfo=None)


@dataclass(frozen=True)
class SMSConfig:
    enabled: bool = False
    timezone: str = 'Europe/Moscow'
    lead_minutes: int = 60

    @classmethod
    def from_env(cls):
        timezone = os.getenv('GUEST_SMS_TIMEZONE', config.training_timezone)
        try:
            ZoneInfo(timezone)
            lead = int(os.getenv('GUEST_SMS_LEAD_MINUTES', '60'))
            if not 15 <= lead <= 1440:
                raise ValueError
        except (ValueError, ZoneInfoNotFoundError):
            return cls(False, config.training_timezone, 60)
        return cls(os.getenv('GUEST_SMS_ENABLED') == '1', timezone, lead)


@dataclass(frozen=True)
class SMSResult:
    # accepted is not delivery confirmation; only delivered confirms delivery.
    state: str
    provider_id: str | None = None


class DisabledSMSProvider:
    configured = False
    idempotent = False

    async def submit(self, **kwargs):
        raise RuntimeError('SMS provider is not configured')

    async def reconcile(self, **kwargs):
        return SMSResult('unknown')


def phone_number(value):
    value = re.sub(r'[ ()-]', '', value or '')
    return value if re.fullmatch(r'\+[1-9][0-9]{7,14}', value) else None


class GuestSMS:
    def __init__(self, engine, *, provider=None, settings=None):
        self.engine = engine
        self.provider = provider or DisabledSMSProvider()
        self.settings = settings or SMSConfig.from_env()

    @property
    def available(self):
        return bool(self.settings.enabled and getattr(self.provider, 'configured', False)
                    and getattr(self.provider, 'idempotent', False))

    async def context(self, session, booking, preference, now):
        if not booking:
            return None, 'cancelled'
        client = await session.get(Client, booking.client_id)
        hotel = await session.get(Hotel, booking.hotel_id)
        if not hotel or not hotel.active or await session.get(Setting, f'booking:trash:{booking.id}'):
            return None, 'cancelled'
        if booking.status not in CONFIRMED_LIFECYCLE:
            return None, 'cancelled' if booking.status in {'CANCELLED', 'REJECTED'} else 'booking_not_confirmed'
        zone = ZoneInfo(preference.timezone if preference else self.settings.timezone)
        moment = datetime.combine(booking.shoot_date, booking.shoot_time).replace(tzinfo=zone).astimezone(UTC)
        if moment <= utc(now):
            return None, 'expired'
        phone = phone_number(client.phone if client else None)
        if not phone:
            return None, 'invalid_phone'
        snapshot = [booking.id, booking.client_id, booking.hotel_id, hotel.name, moment.isoformat(), phone]
        # Archived queues cannot reactivate after a reset/restore epoch change.
        for key in ('operations:generation', 'sales:generation'):
            epoch = await session.get(Setting, key)
            snapshot.append(epoch.value if epoch else '')
        digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False).encode()).hexdigest()
        text = (f'Photo Boss: ваша фотосессия {booking.shoot_date:%d.%m} '
                f'в {booking.shoot_time:%H:%M}, {hotel.name}.')
        return {'at': naive(moment), 'fingerprint': digest, 'phone': phone, 'text': text}, None

    async def configure(self, session, booking, *, enabled, consent, lead_minutes, now=None):
        now = naive(now or datetime.now(UTC))
        if type(enabled) is not bool or type(consent) is not bool or type(lead_minutes) is not int or not 15 <= lead_minutes <= 1440:
            raise ValueError('Проверьте согласие гостя и интервал SMS от 15 до 1440 минут.')
        preference = await session.get(GuestSMSPreference, booking.id)
        if enabled:
            if not self.available:
                raise ValueError('SMS-сервис не подключён. Автоматические SMS отключены.')
            if not consent:
                raise ValueError('Нужно явное согласие гостя на SMS-напоминание.')
            if booking.status != 'CONFIRMED':
                raise ValueError('Сначала подтвердите актуальную дату и время записи.')
            context, reason = await self.context(session, booking, None, now)
            if reason:
                raise ValueError('Проверьте подтверждение записи, телефон и будущее время съёмки.')
        if preference is None:
            preference = GuestSMSPreference(booking_id=booking.id, timezone=self.settings.timezone)
            session.add(preference)
        preference.enabled, preference.consent = enabled, consent if enabled else False
        preference.lead_minutes, preference.timezone, preference.updated_at = lead_minutes, self.settings.timezone, now
        jobs = list(await session.scalars(select(GuestSMSJob).where(GuestSMSJob.booking_id == booking.id)))
        if not enabled:
            for job in jobs:
                if job.state == 'scheduled':
                    job.state, job.updated_at = 'off', now
            return
        preference.confirmed_for = context['at']
        key = hashlib.sha256(('guest-sms:' + context['fingerprint']).encode()).hexdigest()
        existing = next((job for job in jobs if job.event_key == key), None)
        if existing:
            # Repeated PUT, consent toggles, and process restarts cannot resend.
            if existing.state in {'scheduled', 'off'} and existing.attempts == 0:
                existing.due_at = context['at'] - timedelta(minutes=lead_minutes)
                existing.state, existing.next_at = 'scheduled', max(existing.due_at, now)
            return
        for job in jobs:
            if job.state == 'scheduled':
                job.state, job.updated_at = 'rescheduled', now
        due = context['at'] - timedelta(minutes=lead_minutes)
        session.add(GuestSMSJob(booking_id=booking.id, event_key=key, fingerprint=context['fingerprint'],
            appointment_at=context['at'], due_at=due, next_at=max(due, now), state='scheduled'))

    async def status(self, session, booking, *, can_edit=False, now=None):
        preference = await session.get(GuestSMSPreference, booking.id)
        job = await session.scalar(select(GuestSMSJob).where(GuestSMSJob.booking_id == booking.id)
                                   .order_by(GuestSMSJob.id.desc()).limit(1))
        context, reason = await self.context(session, booking, preference, now or datetime.now(UTC))
        state = job.state if job else 'off'
        if job and job.state == 'scheduled' and (reason or context['fingerprint'] != job.fingerprint):
            state = 'expired' if reason == 'expired' else 'cancelled' if reason == 'cancelled' else 'rescheduled'
            reason = reason or 'booking_changed'
        if not self.available:
            state, reason = 'unavailable', 'provider_not_configured'
        client = await session.get(Client, booking.client_id)
        return {'available': self.available, 'enabled': bool(preference and preference.enabled),
            'consent': bool(preference and preference.consent), 'status': state, 'reason': reason,
            'leadMinutes': preference.lead_minutes if preference else self.settings.lead_minutes,
            'timezone': preference.timezone if preference else self.settings.timezone, 'canEdit': can_edit,
            'scheduledFor': utc(job.due_at).isoformat() if job else None,
            'updatedAt': utc(job.updated_at if job else preference.updated_at).isoformat() if job or preference else None,
            'hasPhone': phone_number(client.phone if client else None) is not None}

    async def retry(self, session, booking, *, now=None):
        if not self.available:
            raise ValueError('SMS-сервис не подключён.')
        job = await session.scalar(select(GuestSMSJob).where(GuestSMSJob.booking_id == booking.id)
                                   .order_by(GuestSMSJob.id.desc()).limit(1).with_for_update())
        if not job or job.state != 'failed' or not job.safe_retry or job.attempts >= 3:
            raise ValueError('Повтор запрещён без подтверждения сервиса, что SMS не отправлено.')
        preference = await session.get(GuestSMSPreference, booking.id)
        context, reason = await self.context(session, booking, preference, now or datetime.now(UTC))
        if (not preference or not preference.enabled or not preference.consent or reason
                or context['fingerprint'] != job.fingerprint or preference.confirmed_for != context['at']):
            raise ValueError('Запись изменилась. Нужно новое подтверждение и согласие гостя.')
        job.state, job.next_at, job.safe_retry = 'scheduled', naive(now or datetime.now(UTC)), False

    async def process_one(self, job_id, *, now=None, after_claim=None):
        if not self.available:
            return
        fixed_clock = now is not None
        now = naive(now or datetime.now(UTC))
        async with AsyncSession(self.engine) as session:
            job = await session.get(GuestSMSJob, job_id, with_for_update=True)
            if not job or job.state in TERMINAL or job.state == 'failed' or job.next_at > now:
                return
            if job.state == 'processing':
                if job.lease_until and job.lease_until > now:
                    return
                job.state, job.next_at = 'unknown', now
                await session.commit()
                return
            reconcile = job.state in {'unknown', 'provider_accepted'}
            if not reconcile:
                job.state, job.lease_until, job.next_at = 'processing', now + timedelta(seconds=30), now + timedelta(seconds=30)
                job.attempts += 1
            else:
                # Lease reconciliation too: concurrent workers must not race a
                # terminal result with an older unknown response.
                job.next_at = now + timedelta(seconds=30)
            booking_id = job.booking_id
            await session.commit()
        if after_claim:
            await after_claim()
        async with AsyncSession(self.engine) as session:
            booking = await session.get(Booking, booking_id, with_for_update=True)
            job = await session.get(GuestSMSJob, job_id, with_for_update=True)
            if not job:
                return
            preference = await session.get(GuestSMSPreference, booking_id)
            dispatch_at = now if fixed_clock else naive(datetime.now(UTC))
            context, reason = await self.context(session, booking, preference, dispatch_at)
            valid = (preference and preference.enabled and preference.consent and not reason
                     and preference.confirmed_for == context['at'] and job.fingerprint == context['fingerprint'])
            if not reconcile and not valid:
                job.state = 'expired' if reason == 'expired' else 'cancelled' if reason == 'cancelled' else 'rescheduled'
                await session.commit()
                return
            try:
                if reconcile:
                    result = await asyncio.wait_for(self.provider.reconcile(event_key=job.event_key,
                        provider_id=job.provider_id), 10)
                else:
                    # Booking lock linearizes dispatch against cancellation,
                    # rescheduling, contact edits and opt-out at this moment.
                    result = await asyncio.wait_for(self.provider.submit(phone=context['phone'],
                        text=context['text'], event_key=job.event_key), 10)
            except Exception:  # noqa: BLE001 - ambiguous outcome must never cause automatic resubmission
                result = SMSResult('unknown')
            states = {'accepted': 'provider_accepted', 'delivered': 'delivered',
                      'not_sent': 'failed', 'rejected': 'failed', 'unknown': 'unknown'}
            job.state = states.get(result.state, 'unknown')
            job.safe_retry = result.state == 'not_sent'
            job.provider_id = result.provider_id[:200] if isinstance(result.provider_id, str) else job.provider_id
            job.next_at, job.updated_at, job.lease_until = now + timedelta(minutes=5), now, None
            await session.commit()

    async def tick(self, *, now=None):
        if not self.available:
            return 0
        fixed_clock = now
        now = naive(now or datetime.now(UTC))
        async with AsyncSession(self.engine) as session:
            ids = list(await session.scalars(select(GuestSMSJob.id).where(
                GuestSMSJob.state.in_({'scheduled', 'processing', 'provider_accepted', 'unknown'}),
                GuestSMSJob.next_at <= now).order_by(GuestSMSJob.next_at, GuestSMSJob.id).limit(50)))
        for job_id in ids:
            async with operation_lease(self.engine):
                await self.process_one(job_id, now=fixed_clock)
        return len(ids)


async def guest_sms_loop(engine, *, service=None, interval=30):
    service = service or GuestSMS(engine)
    while True:
        try:
            await service.tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - no provider/contact data in logs
            log.warning('Guest SMS cycle deferred (%s)', type(exc).__name__)
        await asyncio.sleep(interval)
