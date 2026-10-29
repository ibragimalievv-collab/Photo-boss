"""Only injected SMS transports; never credentials, live accounts, or real SMS."""
import asyncio
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_workflow_services import fixture

from app.models import Booking, Client, GuestSMSJob, GuestSMSPreference, Setting
from app.services.guest_sms import GuestSMS, SMSConfig, SMSResult

NOW = datetime(2030, 1, 1, 8, tzinfo=UTC)


async def setup():
    engine, factory = await fixture()
    async with factory() as session:
        client = Client(name='Synthetic Guest', phone='+79990000000')
        session.add(client)
        await session.flush()
        booking = Booking(client_id=client.id, hotel_id=1, package_id=1, room='TEST',
            manager_id=1, photographer_id=2, shoot_date=NOW.date(),
            shoot_time=clock_time(12), status='CONFIRMED')
        session.add(booking)
        await session.commit()
        bid = booking.id
    provider = SimpleNamespace(configured=True, idempotent=True,
        submit=AsyncMock(return_value=SMSResult('accepted', 'synthetic-id')),
        reconcile=AsyncMock(return_value=SMSResult('unknown')))
    service = GuestSMS(engine, provider=provider, settings=SMSConfig(True))
    return engine, factory, bid, provider, service


async def opt_in(factory, bid, service):
    async with factory() as session:
        booking = await session.get(Booking, bid, with_for_update=True)
        await service.configure(session, booking, enabled=True, consent=True, lead_minutes=60, now=NOW)
        await session.commit()
        return await session.scalar(select(GuestSMSJob.id))


def test_default_disabled_even_with_opt_in_env_and_no_automatic_backfill(monkeypatch):
    monkeypatch.setenv('GUEST_SMS_ENABLED', '1')
    async def run():
        engine, factory, bid, provider, _ = await setup()
        try:
            service = GuestSMS(engine)
            assert not service.available and await service.tick(now=NOW) == 0
            async with factory() as session:
                booking = await session.get(Booking, bid)
                status = await service.status(session, booking, now=NOW)
                assert status['status'] == 'unavailable' and not status['consent']
                assert '+7999' not in str(status) and 'phone' not in status
                with pytest.raises(ValueError, match='не подключён'):
                    await service.configure(session, booking, enabled=True, consent=True, lead_minutes=60, now=NOW)
                assert await session.scalar(select(func.count(GuestSMSJob.id))) == 0
                assert await session.scalar(select(func.count(GuestSMSPreference.booking_id))) == 0
            provider.submit.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_confirmed_lifecycle_idempotency_and_provider_accepted_is_not_delivery():
    async def run():
        engine, factory, bid, provider, service = await setup()
        try:
            await opt_in(factory, bid, service)
            await opt_in(factory, bid, service)
            async with factory() as session:
                assert await session.scalar(select(func.count(GuestSMSJob.id))) == 1
                (await session.get(Booking, bid)).status = 'PICKED_UP'
                await session.commit()
            await service.tick(now=NOW)
            async with factory() as session:
                job = await session.scalar(select(GuestSMSJob))
                assert job.state == 'provider_accepted' and job.attempts == 1
                assert job.appointment_at.replace(tzinfo=UTC) == datetime(2030, 1, 1, 9, tzinfo=UTC)
            provider.reconcile.return_value = SMSResult('delivered', 'synthetic-id')
            await GuestSMS(engine, provider=provider, settings=SMSConfig(True)).tick(now=NOW + timedelta(minutes=6))
            await service.tick(now=NOW + timedelta(minutes=7))
            async with factory() as session:
                assert (await session.scalar(select(GuestSMSJob))).state == 'delivered'
            provider.submit.assert_awaited_once()
            assert provider.submit.await_args.kwargs['phone'] == '+79990000000'
            assert '12:00' in provider.submit.await_args.kwargs['text']
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('status', ['NEW', 'PENDING_CONFIRMATION', 'RESCHEDULED', 'SHOOTING', 'CANCELLED'])
def test_consent_never_confirms_a_booking(status):
    async def run():
        engine, factory, bid, provider, service = await setup()
        try:
            async with factory() as session:
                booking = await session.get(Booking, bid)
                booking.status = status
                with pytest.raises(ValueError, match='подтвердите'):
                    await service.configure(session, booking, enabled=True, consent=True, lead_minutes=60, now=NOW)
                assert await session.scalar(select(func.count(GuestSMSJob.id))) == 0
            provider.submit.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['cancel', 'reschedule', 'phone', 'opt_out', 'reset_epoch'])
def test_current_state_is_rechecked_after_claim_before_dispatch(change):
    async def run():
        engine, factory, bid, provider, service = await setup()
        try:
            job_id = await opt_in(factory, bid, service)
            async def after_claim():
                async with factory() as session:
                    booking = await session.get(Booking, bid)
                    if change == 'cancel':
                        booking.status = 'CANCELLED'
                    elif change == 'reschedule':
                        booking.shoot_date += timedelta(days=1)
                        booking.status = 'RESCHEDULED'
                    elif change == 'phone':
                        (await session.get(Client, booking.client_id)).phone = '+79990000001'
                    elif change == 'opt_out':
                        (await session.get(GuestSMSPreference, bid)).consent = False
                    else:
                        session.add(Setting(key='operations:generation', value='new-reset-or-restore-epoch'))
                    await session.commit()
            await service.process_one(job_id, now=NOW, after_claim=after_claim)
            provider.submit.assert_not_awaited()
            async with factory() as session:
                assert (await session.get(GuestSMSJob, job_id)).state in {'cancelled', 'rescheduled'}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_ambiguous_response_cannot_retry_until_definitely_not_sent_reconciliation():
    async def run():
        engine, factory, bid, provider, service = await setup()
        try:
            job_id = await opt_in(factory, bid, service)
            provider.submit.side_effect = [TimeoutError(), SMSResult('accepted', 'retry-id')]
            await service.process_one(job_id, now=NOW)
            async with factory() as session:
                assert (await session.get(GuestSMSJob, job_id)).state == 'unknown'
                with pytest.raises(ValueError, match='Повтор запрещён'):
                    await service.retry(session, await session.get(Booking, bid), now=NOW)
            await GuestSMS(engine, provider=provider, settings=SMSConfig(True)).tick(now=NOW + timedelta(minutes=6))
            assert provider.submit.await_count == 1
            provider.reconcile.return_value = SMSResult('not_sent')
            await service.tick(now=NOW + timedelta(minutes=12))
            async with factory() as session:
                await service.retry(session, await session.get(Booking, bid), now=NOW + timedelta(minutes=12))
                await session.commit()
            await service.tick(now=NOW + timedelta(minutes=12))
            assert provider.submit.await_count == 2
            assert provider.submit.await_args_list[0].kwargs['event_key'] == provider.submit.await_args_list[1].kwargs['event_key']
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_process_crash_after_submit_restarts_as_unknown_without_resending():
    async def run():
        engine, factory, bid, provider, service = await setup()
        try:
            job_id = await opt_in(factory, bid, service)
            provider.submit.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await service.process_one(job_id, now=NOW)
            async with factory() as session:
                assert (await session.get(GuestSMSJob, job_id)).state == 'processing'
            restarted = GuestSMS(engine, provider=provider, settings=SMSConfig(True))
            await restarted.tick(now=NOW + timedelta(seconds=31))
            provider.reconcile.return_value = SMSResult('delivered', 'recovered-id')
            await restarted.tick(now=NOW + timedelta(seconds=32))
            async with factory() as session:
                assert (await session.get(GuestSMSJob, job_id)).state == 'delivered'
            provider.submit.assert_awaited_once()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_past_missing_phone_and_missing_consent_fail_closed():
    async def run():
        engine, factory, bid, provider, service = await setup()
        try:
            async with factory() as session:
                booking = await session.get(Booking, bid)
                with pytest.raises(ValueError, match='согласие'):
                    await service.configure(session, booking, enabled=True, consent=False, lead_minutes=60, now=NOW)
                with pytest.raises(ValueError, match='будущее'):
                    await service.configure(session, booking, enabled=True, consent=True, lead_minutes=60, now=NOW + timedelta(hours=2))
                (await session.get(Client, booking.client_id)).phone = 'not a phone'
                with pytest.raises(ValueError, match='телефон'):
                    await service.configure(session, booking, enabled=True, consent=True, lead_minutes=60, now=NOW)
            provider.submit.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_sms_api_uses_current_booking_scope_and_never_returns_phone():
    async def run():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from test_miniapp_release import TOKEN, signed

        from app.miniapp_api import install_miniapp
        from app.models import User, UserRole
        engine, factory, bid, _, _ = await setup()
        async with factory() as session:
            session.add_all([User(id=3, tg_id=3, name='Unrelated'), UserRole(user_id=3, role='MANAGER')])
            await session.commit()
        bot = SimpleNamespace(token=TOKEN)
        app = web.Application()
        install_miniapp(app, engine=engine, bot=bot, lessons=[])
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            path = f'/api/miniapp/bookings/{bid}/sms-reminder'
            headers = lambda uid: {'X-Telegram-Init-Data': signed(uid)}
            response = await client.get(path, headers=headers(1))
            assert response.status == 200
            body = await response.json()
            assert body['status'] == 'unavailable' and body['canEdit'] and not body['consent']
            assert '+7999' not in str(body) and 'providerId' not in body
            assert (await client.put(path, headers=headers(1), json={
                'enabled': True, 'consent': True, 'leadMinutes': 60})).status == 409
            assert (await client.get(path, headers=headers(3))).status == 403
            response = await client.get(path, headers=headers(2))
            assert response.status == 200 and not (await response.json())['canEdit']
            assert (await client.put(path, headers=headers(2), json={
                'enabled': False, 'consent': False, 'leadMinutes': 60})).status == 403
            async with factory() as session:
                assert await session.scalar(select(func.count(GuestSMSJob.id))) == 0
                (await session.get(User, 1)).active = False
                await session.commit()
            assert (await client.get(path, headers=headers(1))).status == 403
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_legacy_create_all_and_reset_dependencies_are_additive_and_empty_optout():
    async def run():
        from app.db import Base
        from app.operational_reset import _restored_blocks_match, _snapshot
        from app.work_history_reset import business_rows
        engine, factory, bid, _, service = await setup()
        try:
            # A pre-SMS database receives empty additive tables, not opt-ins.
            async with engine.begin() as connection:
                await connection.run_sync(GuestSMSJob.__table__.drop)
                await connection.run_sync(GuestSMSPreference.__table__.drop)
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                assert await session.scalar(select(func.count(GuestSMSPreference.booking_id))) == 0
            await opt_in(factory, bid, service)
            async with engine.begin() as connection:
                tables = Base.metadata.tables
                # Operational reset recognizes both FK children explicitly.
                await _snapshot(connection, tables)
                before = {name: list((await connection.execute(select(table))).mappings())
                          for name, table in tables.items()}
                selected = business_rows(before, tables)
                assert len(selected['guest_sms_jobs']) == len(selected['guest_sms_preferences']) == 1
            assert _restored_blocks_match({'guest_sms_jobs': {'columns': ['id'], 'rows': []}}, {})
        finally:
            await engine.dispose()
    asyncio.run(run())
