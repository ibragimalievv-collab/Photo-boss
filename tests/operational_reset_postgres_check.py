"""Reset/restore, immutability and real cross-process locks in a disposable schema."""
import asyncio
import os
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db import Base
from app.models import Booking
from app.operational_reset import ResetError, preview, reset, restore
from app.operations_guard import (
    ADVISORY_KEY,
    _lease_engines,
    close_operation_leases,
    operation_lease,
)


async def wait_for_lock(engine, mode):
    """Observe the actual advisory waiter, not an assumed timing delay."""
    async with asyncio.timeout(10):
        while True:
            async with engine.connect() as conn:
                waiting = await conn.scalar(text("""SELECT count(*) FROM pg_locks
                    WHERE locktype='advisory' AND NOT granted AND mode=:mode
                    AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                    AND classid::bigint=:high AND objid::bigint=:low AND objsubid=1"""),
                    {'mode': mode, 'high': ADVISORY_KEY >> 32, 'low': ADVISORY_KEY & 0xffffffff})
            if waiting:
                return
            await asyncio.sleep(.02)


async def seed(engine):
    tables = Base.metadata.tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Legacy/reflected columns must survive without ORM knowledge or lossy JSON coercion.
        await conn.exec_driver_sql("""ALTER TABLE bookings
            ADD COLUMN legacy_blob BYTEA, ADD COLUMN legacy_decimal NUMERIC(12,2),
            ADD COLUMN legacy_uuid UUID, ADD COLUMN legacy_json JSONB,
            ADD COLUMN legacy_datetime TIMESTAMPTZ""")

        async def put(table_name, **data):
            return await conn.execute(tables[table_name].insert().values(**data).returning(*tables[table_name].primary_key))

        await put('users', id=1, tg_id=8000000001, name='Owner')
        await put('users', id=2, tg_id=8000000002, name='Photographer')
        await put('user_roles', user_id=1, role='OWNER')
        await put('user_roles', user_id=2, role='PHOTOGRAPHER')
        await put('hotels', id=1, name='Retained hotel')
        await put('hotel_employees', hotel_id=1, user_id=2)
        await put('clients', id=1, name='Retained guest')
        await put('packages', id=1, name='Retained tariff', price_per_photo=400)
        booking_id = (await put('bookings', hotel_id=1, client_id=1, package_id=1,
            manager_id=1, photographer_id=2, room='Old', shoot_date=date(2026, 10, 16),
            shoot_time=time(12, 13, 14, 500), status='COMPLETED')).scalar_one()
        await conn.execute(text("""UPDATE bookings SET legacy_blob=:blob, legacy_decimal=:amount,
            legacy_uuid=:uid, legacy_json=CAST(:payload AS JSONB), legacy_datetime=:moment WHERE id=:id"""),
            {'blob': b'\x00\xffrecoverable', 'amount': Decimal('12345.67'), 'uid': uuid.uuid4(),
             'payload': '{"tag":["datetime","not-a-type-tag"],"nested":{"bool":true}}',
             'moment': datetime(2026, 10, 16, 12, 13, 14, 500, tzinfo=UTC), 'id': booking_id})
        shooting_id = (await put('shootings', booking_id=booking_id)).scalar_one()
        photo_id = (await put('photos', shooting_id=shooting_id, file_id='preserved-physical-file')).scalar_one()
        await put('photo_storage', photo_id=photo_id, shooting_id=shooting_id,
                  telegram_file_id='preserved-physical-file', telegram_unique_id='preserved-unique',
                  source_kind='DOCUMENT', disk_path='/original.jpg')
        await put('academy_reviews', user_id=2, photo_id=photo_id, file_id='preserved-physical-file',
                  composition_score=8, light_score=8, pose_score=8, emotion_score=8,
                  color_score=8, quality_score=8, recommendation='Retain Academy assessment')
        await put('sales', booking_id=booking_id, created_by_id=2, credited_user_id=2,
                  sold_photos=2, amount=800.25, percent=15, commission=120.0375)
        await put('receipts', booking_id=booking_id, uploaded_by_id=2, purpose='PAYMENT',
                  file_id='receipt', file_unique_id='receipt', expected_amount=Decimal('800.25'),
                  verified_amount=Decimal('800.25'), status='APPROVED')
        await put('payroll_entries', user_id=2, kind='Штраф за опоздание', amount=-500, period='2026-10')
        await put('payroll_entries', user_id=2, kind='Ручная премия', amount=1000, period='2026-10')
        await put('shift_check_ins', user_id=2, shift_date=date(2026, 10, 16), late=True, fine_amount=500)
        await put('shift_check_outs', user_id=2, shift_date=date(2026, 10, 16), report_note='Old report')
        await put('shifts', user_id=2, hotel_id=1,
                  start_at=datetime(2026, 10, 16, 8, tzinfo=UTC).replace(tzinfo=None),
                  end_at=datetime(2026, 10, 16, 20, tzinfo=UTC).replace(tzinfo=None))
        before = dict((await conn.execute(text('SELECT * FROM bookings'))).mappings().one())
        sequence = (await conn.execute(text('SELECT last_value,is_called FROM bookings_id_seq'))).one()
    return before, sequence


async def main():
    url = os.environ['BLOCKER_POSTGRES_TEST_URL']
    assert make_url(url).host in {'localhost', '127.0.0.1'}, 'Only a local disposable test database is allowed'
    assert ADVISORY_KEY == 7620182632501, 'Reset and mutation leases must share the established advisory key'
    schema = 'pb_operations_reset_' + uuid.uuid4().hex
    admin_engine = create_async_engine(url)
    async with admin_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    connect_args = {'server_settings': {'search_path': schema, 'statement_timeout': '15000'}}
    engine = create_async_engine(url, connect_args=connect_args)
    # The real lease function uses an independent unpooled engine. Supply the same
    # disposable search_path, so this test cannot touch the database's public schema.
    _lease_engines[engine] = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    try:
        before, sequence = await seed(engine)
        report = await preview(engine)
        lease_acquired, release_lease = asyncio.Event(), asyncio.Event()

        async def hold_live_operation():
            async with operation_lease(engine):
                lease_acquired.set()
                await release_lease.wait()

        live = asyncio.create_task(hold_live_operation())
        await asyncio.wait_for(lease_acquired.wait(), 10)
        resetting = asyncio.create_task(reset(engine, 'postgres-reset-request-0001', 1,
            expected_preview_sha256=report['snapshot_sha256']))
        try:
            await wait_for_lock(engine, 'ExclusiveLock')
            assert not resetting.done(), 'Reset bypassed an in-flight shared operation lease'
        finally:
            release_lease.set()
        _, first = await asyncio.wait_for(asyncio.gather(live, resetting), 20)
        assert not first['replayed'] and (await preview(engine))['total_deleted_rows'] == 0
        async with engine.connect() as conn:
            assert (await conn.execute(text('SELECT last_value,is_called FROM bookings_id_seq'))).one() == sequence
            assert await conn.scalar(text('SELECT count(*) FROM users')) == 2
            assert await conn.scalar(text('SELECT count(*) FROM shifts')) == 1
            assert await conn.scalar(text('SELECT count(*) FROM payroll_entries')) == 1
            assert await conn.scalar(text('SELECT photo_id FROM academy_reviews')) is None
        for sql in ('UPDATE operational_reset_archives SET payload=\'{}\'',
                    'DELETE FROM operational_reset_archives', 'TRUNCATE operational_reset_archives CASCADE'):
            try:
                async with engine.begin() as conn:
                    await conn.exec_driver_sql(sql)
            except DBAPIError as exc:
                assert 'immutable' in str(exc).lower(), f'Unexpected archive protection failure: {exc}'
            else:
                raise AssertionError('Recovery archive accepted mutation: ' + sql)
        restored = await restore(engine, 'postgres-reset-request-0001', 1)
        assert not restored['replayed'] and restored['generation'] != first['generation']
        async with engine.connect() as conn:
            assert dict((await conn.execute(text('SELECT * FROM bookings'))).mappings().one()) == before
            assert await conn.scalar(text('SELECT photo_id FROM academy_reviews')) is not None
            assert await conn.scalar(text('SELECT verified_amount FROM receipts')) == Decimal('800.25')
            assert (await conn.execute(text('SELECT last_value,is_called FROM bookings_id_seq'))).one() == sequence
        second = await reset(engine, 'postgres-reset-request-0002', 1)
        acquired = asyncio.Event()

        async def create_fresh_booking():
            async with operation_lease(engine):
                acquired.set()
                async with engine.begin() as conn:
                    return await conn.scalar(Booking.__table__.insert().values(hotel_id=1, client_id=1,
                        package_id=1, manager_id=1, room='Fresh', shoot_date=date(2026, 10, 18),
                        shoot_time=time(15)).returning(Booking.id))

        async with engine.begin() as conn:
            await conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': ADVISORY_KEY})
            creating = asyncio.create_task(create_fresh_booking())
            await wait_for_lock(engine, 'ShareLock')
            assert not acquired.is_set(), 'A new operation bypassed the exclusive reset lease'
        new_id = await asyncio.wait_for(creating, 10)
        assert new_id > before['id'], 'Reset reused an archived identity/media path'
        repeated = await reset(engine, 'postgres-reset-request-0002', 1)
        assert repeated['replayed'] and repeated['archive_sha256'] == second['archive_sha256']
        try:
            await restore(engine, 'postgres-reset-request-0002', 1)
        except ResetError as exc:
            assert 'New operational work' in str(exc)
        else:
            raise AssertionError('Restore accepted fresh operational records')
        async with engine.connect() as conn:
            assert (await conn.execute(text('SELECT id,room FROM bookings'))).one() == (new_id, 'Fresh')
            assert await conn.scalar(text('SELECT count(*) FROM operational_reset_archives')) == 2
            assert await conn.scalar(text('SELECT count(*) FROM operational_reset_restores')) == 1
        print('PASS: PostgreSQL shared/exclusive reset barrier, typed exact restore, immutable archives, retained sequences and replay preserving fresh work')
    finally:
        await close_operation_leases(engine)
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
