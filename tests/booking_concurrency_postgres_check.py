"""Concurrent guests/replays/rate changes against a disposable PostgreSQL schema."""
import asyncio
import json
import os
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.hotel_pricing import HotelPricing
from app.models import (
    Booking,
    Client,
    Hotel,
    OperationRequest,
    Package,
    Shooting,
    User,
    UserRole,
)
from app.services import pricing
from app.workday import Workday


class Request(dict):
    def __init__(self, actor, body):
        super().__init__(miniapp_actor=actor)
        self.body = body


def packet(uid, suffix='guest'):
    return {'actorId': uid, 'key': f'parallel-booking-{uid}-{suffix}', 'kind': 'booking',
            'date': '2026-09-22', 'data': {
                'client_name': f'CI Guest {uid}', 'client_phone': None, 'hotel_id': 1,
                'package_id': 1, 'room': str(uid), 'guest_count': 2, 'deposit': '0',
                'shoot_date': '2026-09-23', 'shoot_time': '12:00', 'photographer_id': None}}


async def main():
    # No DATABASE_URL fallback: this must never point at the live application.
    schema = 'pb_booking_concurrency_' + uuid4().hex
    engine = create_async_engine(os.environ['UPDATE_POSTGRES_TEST_URL'],
        connect_args={'server_settings': {'search_path': schema}}, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def rows(conn, sql, **params):
        return list((await conn.execute(text(sql), params)).mappings())

    async def body(request):
        return request.body

    api = SimpleNamespace(engine=engine, rows=rows, body=body, today=lambda: date(2026, 9, 22),
                          day=date.fromisoformat)
    work = Workday(api)
    actors = {uid: {'id': uid, 'roles': ['MANAGER']} for uid in range(1, 13)}
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all([User(id=uid, tg_id=uid, name=f'CI Manager {uid}') for uid in actors])
            session.add(User(id=99, tg_id=99, name='CI Owner'))
            await session.flush()
            session.add_all([UserRole(user_id=uid, role='MANAGER') for uid in actors])
            session.add_all([UserRole(user_id=99, role='OWNER'), Hotel(id=1, name='CI Hotel'),
                             Package(id=1, name='CI Standard', price_per_photo=400)])
            await session.commit()

        entered, release = asyncio.Event(), asyncio.Event()
        original_quote = pricing.booking_unit_quote
        first = True

        async def paused_quote(session, hotel_id, package_id):
            nonlocal first
            quote = await original_quote(session, hotel_id, package_id)
            if first:
                first = False
                entered.set()
                await release.wait()
            return quote

        with patch.object(pricing, 'booking_unit_quote', paused_quote):
            slow = asyncio.create_task(work.execute(actors[1], packet(1)))
            await asyncio.wait_for(entered.wait(), 3)
            other = asyncio.create_task(work.execute(actors[2], packet(2)))
            try:
                # The first transaction is intentionally still open. Different
                # managers at the same hotel must not queue behind its snapshot.
                second = await asyncio.wait_for(asyncio.shield(other), 3)
                assert second.status == 200
                assert not slow.done()
            finally:
                release.set()
                await asyncio.gather(slow, other)
        print('PASS PostgreSQL: another manager records a guest while the first hotel snapshot remains open')

        results = await asyncio.wait_for(asyncio.gather(*(
            work.execute(actors[uid], packet(uid)) for uid in range(3, 13)
        )), 10)
        ids = {json.loads(result.text)['bookingId'] for result in results}
        assert len(ids) == 10
        replays = await asyncio.wait_for(asyncio.gather(*(
            work.execute(actors[3], packet(3)) for _ in range(8)
        )), 10)
        assert len({result.text for result in replays}) == 1
        async with factory() as session:
            for model in (Booking, Client, Shooting, OperationRequest):
                assert await session.scalar(select(func.count()).select_from(model)) == 12
        print('PASS PostgreSQL: 12 managers/one hotel and 8 concurrent replays create exactly 12 guests/bookings/shootings/acknowledgements')

        # Shared snapshots must still block an exclusive rate replacement.
        entered.clear()
        release.clear()
        first = True
        save_started = asyncio.Event()
        owner = {'id': 99, 'roles': ['OWNER']}
        request = Request(owner, {'hotelId': 1, 'name': 'CI New Rate', 'kind': 'UNIT',
                                  'price': '500', 'photoCount': None, 'replaces': None})
        async def update_rate():
            save_started.set()
            return await HotelPricing(api).save(request)

        with patch.object(pricing, 'booking_unit_quote', paused_quote):
            old_booking = asyncio.create_task(work.execute(actors[1], packet(1, 'before-rate')))
            await asyncio.wait_for(entered.wait(), 3)
            changing = asyncio.create_task(update_rate())
            await save_started.wait()
            try:
                # Observe the actual PostgreSQL lock wait instead of relying on
                # a sleep or a scheduler ordering assumption.
                async with asyncio.timeout(3):
                    while True:
                        async with engine.connect() as conn:
                            waiting = await conn.scalar(text("""SELECT count(*) FROM pg_stat_activity
                                WHERE datname=current_database() AND wait_event_type='Lock'
                                AND query ILIKE '%hotels%' AND pid<>pg_backend_pid()"""))
                        if waiting:
                            break
                        await asyncio.sleep(.01)
                assert not changing.done()
            finally:
                release.set()
                old_result, _ = await asyncio.gather(old_booking, changing)
        new_result = await work.execute(actors[2], packet(2, 'after-rate'))
        async with factory() as session:
            before = await session.get(Booking, json.loads(old_result.text)['bookingId'])
            after = await session.get(Booking, json.loads(new_result.text)['bookingId'])
            assert json.loads(before.pricing_snapshot)['price'] == '400.00'
            assert json.loads(after.pricing_snapshot)['price'] == '500.00'
        print('PASS PostgreSQL: tariff update waits for earlier booking snapshot; later booking receives the new rate')

        # Force a real deadlock *inside Workday*, after provisional booking
        # inserts. The victim's fresh-transaction retry must not leave a guest,
        # shooting, audit acknowledgement or booking from the rolled-back try.
        from app import workflow
        original_create = workflow.create_booking_record
        ready = {uid: asyncio.Event() for uid in (4, 5)}
        attempts = {4: 0, 5: 0}

        async def conflicting_create(session, actor, data, photographer_id=None):
            attempts[actor.id] += 1
            booking = await original_create(session, actor, data, photographer_id)
            if attempts[actor.id] == 1:
                ready[actor.id].set()
                await ready[9-actor.id].wait()
            await session.execute(text('SELECT id FROM users WHERE id=:uid FOR NO KEY UPDATE'),
                                  {'uid': 9-actor.id})
            return booking

        with patch.object(workflow, 'create_booking_record', conflicting_create):
            recovered = await asyncio.wait_for(asyncio.gather(*(
                work.execute(actors[uid], packet(uid, 'deadlock')) for uid in (4, 5)
            )), 10)
        assert sorted(attempts.values()) == [1, 2], attempts
        assert len({json.loads(result.text)['bookingId'] for result in recovered}) == 2
        async with factory() as session:
            for model in (Booking, Client, Shooting, OperationRequest):
                assert await session.scalar(select(func.count()).select_from(model)) == 16
        print('PASS PostgreSQL: real deadlock rolls provisional guest/booking back and retries once; exactly two final operations')

        # A supervisor may also be an assigned photographer. Holding their
        # identity stable during a tariff edit must not block another manager's
        # booking/assignment FK inserts to that same user.
        from app import hotel_pricing
        async with factory() as session:
            session.add_all([UserRole(user_id=6, role='OWNER'),
                             UserRole(user_id=99, role='PHOTOGRAPHER')])
            await session.commit()
        pricing_entered, release_pricing = asyncio.Event(), asyncio.Event()
        original_access = hotel_pricing.hotel_access

        async def paused_access(*args, **kwargs):
            result = await original_access(*args, **kwargs)
            pricing_entered.set()
            await release_pricing.wait()
            return result

        with patch.object(hotel_pricing, 'hotel_access', paused_access):
            changing = asyncio.create_task(HotelPricing(api).save(Request(owner,
                request.body | {'name': 'CI Last Rate', 'price': '300'})))
            await asyncio.wait_for(pricing_entered.wait(), 3)
            assigned_body = packet(6, 'assigned-during-rate')
            assigned_body['data']['photographer_id'] = 99
            assigned = asyncio.create_task(work.execute(actors[6] | {'roles': ['OWNER']}, assigned_body))
            try:
                assigned_result = await asyncio.wait_for(asyncio.shield(assigned), 3)
                assert assigned_result.status == 200 and not changing.done()
            finally:
                release_pricing.set()
                await asyncio.gather(changing, assigned)
        async with factory() as session:
            assigned_booking = await session.get(Booking, json.loads(assigned_result.text)['bookingId'])
            assert assigned_booking.photographer_id == 99
            assert await session.scalar(select(func.count(OperationRequest.id))) == 17
        print('PASS PostgreSQL: supervisor tariff identity lock does not block concurrent booking/assignment references')
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
