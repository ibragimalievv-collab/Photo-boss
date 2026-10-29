"""Optional real Postgres lock test, restricted to the dedicated local audit DB."""
import asyncio
import os
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import Base
from app.models import Booking, Client, GuestSMSJob, Hotel, Package, User
from app.services.guest_sms import GuestSMS, SMSConfig, SMSResult


def test_postgres_concurrent_workers_claim_once_and_cancel_after_claim():
    raw = os.environ.get('GUEST_SMS_TEST_POSTGRES_URL')
    if not raw:
        pytest.skip('Dedicated local Postgres not requested')
    url = make_url(raw)
    assert url.drivername == 'postgresql+asyncpg' and url.host in {'127.0.0.1', 'localhost'} and url.port == 55439
    async def run():
        schema = 'pb_guest_sms_' + uuid4().hex
        engine = create_async_engine(url, connect_args={'server_settings': {
            'search_path': schema, 'application_name': schema}})
        now = datetime(2030, 1, 1, 8, tzinfo=UTC)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                assert await connection.scalar(text('SELECT current_schema()')) == schema
                await connection.run_sync(Base.metadata.create_all)
            async with AsyncSession(engine) as session:
                session.add_all([User(id=1, tg_id=1, name='Synthetic'), Hotel(id=1, name='Synthetic'),
                    Client(id=1, name='Synthetic', phone='+79990000000'), Package(id=1, name='Synthetic', price_per_photo=400)])
                await session.flush()
                session.add(Booking(id=1, hotel_id=1, client_id=1, package_id=1, manager_id=1,
                    room='TEST', status='CONFIRMED', shoot_date=now.date(), shoot_time=time(12)))
                await session.commit()
            provider = SimpleNamespace(configured=True, idempotent=True,
                submit=AsyncMock(return_value=SMSResult('accepted', 'mock-id')),
                reconcile=AsyncMock(return_value=SMSResult('unknown')))
            service = GuestSMS(engine, provider=provider, settings=SMSConfig(True))
            async with AsyncSession(engine) as session:
                await service.configure(session, await session.get(Booking, 1),
                    enabled=True, consent=True, lead_minutes=60, now=now)
                await session.commit()
                job_id = await session.scalar(select(GuestSMSJob.id))
            # Both real workers contend for the same durable claim.
            await asyncio.gather(service.process_one(job_id, now=now), service.process_one(job_id, now=now))
            provider.submit.assert_awaited_once()
            async with AsyncSession(engine) as session:
                booking = await session.get(Booking, 1)
                booking.shoot_date += timedelta(days=1)
                await service.configure(session, booking, enabled=True, consent=True, lead_minutes=60, now=now)
                await session.commit()
                new_job = await session.scalar(select(GuestSMSJob.id).order_by(GuestSMSJob.id.desc()))
            async def cancel_after_claim():
                async with AsyncSession(engine) as session:
                    (await session.get(Booking, 1, with_for_update=True)).status = 'CANCELLED'
                    await session.commit()
            await service.process_one(new_job, now=now + timedelta(days=1), after_claim=cancel_after_claim)
            provider.submit.assert_awaited_once()
            async with AsyncSession(engine) as session:
                assert (await session.get(GuestSMSJob, new_job)).state == 'cancelled'
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await engine.dispose()
    asyncio.run(run())
