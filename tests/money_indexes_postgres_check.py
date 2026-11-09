"""Exact money columns and list indexes on a disposable PostgreSQL schema.

Simulates a database from an earlier release (double precision money, no list
indexes), runs the startup upgrade, and checks: exact NUMERIC(14,2) values, the
new indexes, a repeat run, and that a table locked by the previous instance only
postpones the step instead of failing startup. No credentials or provider calls.
"""
import asyncio
import os
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import Base
from app.models import Booking, Client, Hotel, Package, Sale, User
from app.schema_updates import MONEY_COLUMNS, PERFORMANCE_INDEXES, upgrade


async def column_type(conn, table, column):
    row = (await conn.execute(text(
        'SELECT data_type,numeric_precision,numeric_scale FROM information_schema.columns '
        'WHERE table_schema=current_schema() AND table_name=:t AND column_name=:c'),
        {'t': table, 'c': column})).one()
    return tuple(row)


async def index_names(conn):
    return set((await conn.execute(text(
        'SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()'))).scalars())


async def startup_upgrade(engine):
    # The same limits as app.db.init_db while an older instance still serves.
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '2000ms'"))
        await conn.execute(text("SET LOCAL statement_timeout = '30000ms'"))
        await upgrade(conn)


async def main():
    url = os.environ['UPDATE_POSTGRES_TEST_URL']
    schema = 'pb_money_' + uuid4().hex
    options = {'server_settings': {'search_path': schema}}
    engine = create_async_engine(url, connect_args=options)
    blocker = create_async_engine(url, connect_args=options)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
            # Turn the fresh schema into one created by an earlier release.
            for table, column in MONEY_COLUMNS:
                await conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN {column} TYPE DOUBLE PRECISION'))
            for name in PERFORMANCE_INDEXES:
                await conn.execute(text(f'DROP INDEX {name}'))
        async with AsyncSession(engine) as session:
            session.add_all([User(id=1, tg_id=1, name='Synthetic Owner'), Client(id=1, name='Synthetic Guest'),
                             Hotel(id=1, name='Synthetic Hotel'), Package(id=1, name='Synthetic', price_per_photo=400)])
            await session.flush()
            session.add(Booking(id=1, hotel_id=1, client_id=1, package_id=1, room='1', manager_id=1,
                                deposit=1000, shoot_date=__import__('datetime').date(2026, 9, 1),
                                shoot_time=__import__('datetime').time(12)))
            await session.flush()
            # Classic binary leftovers: 0.1 + 0.2 and an unrounded percentage.
            session.add_all([Sale(id=1, booking_id=1, created_by_id=1, credited_user_id=1, sold_photos=1,
                                  amount=0.1 + 0.2, commission=1234.5678),
                             Sale(id=2, booking_id=1, created_by_id=1, credited_user_id=1, sold_photos=3,
                                  amount=1200.0, commission=132.0)])
            await session.commit()

        await startup_upgrade(engine)
        async with engine.connect() as conn:
            for table, column in MONEY_COLUMNS:
                assert await column_type(conn, table, column) == ('numeric', 14, 2), (table, column)
            assert set(PERFORMANCE_INDEXES) <= await index_names(conn)
            rows = (await conn.execute(text('SELECT id,amount,commission FROM sales ORDER BY id'))).all()
            assert rows == [(1, Decimal('0.30'), Decimal('1234.57')), (2, Decimal('1200.00'), Decimal('132.00'))], rows
            assert await conn.scalar(text('SELECT sum(amount) FROM sales')) == Decimal('1200.30')
        # Application code keeps receiving float from the ORM.
        async with AsyncSession(engine) as session:
            assert (await session.scalar(select(Sale.amount).where(Sale.id == 1))) == 0.3
            assert type(await session.scalar(select(Booking.deposit))) is float

        # A repeated start changes nothing.
        await startup_upgrade(engine)

        # The previous instance holds the table: the step waits 2 s, is postponed,
        # startup continues, and the next start finishes it.
        async with engine.begin() as conn:
            await conn.execute(text('ALTER TABLE payroll_entries ALTER COLUMN amount TYPE DOUBLE PRECISION'))
            await conn.execute(text('DROP INDEX ix_audit_logs_created_at'))
        async with blocker.connect() as held:
            transaction = await held.begin()
            await held.execute(text('LOCK TABLE payroll_entries IN ROW EXCLUSIVE MODE'))
            await held.execute(text('LOCK TABLE audit_logs IN ROW EXCLUSIVE MODE'))
            await asyncio.wait_for(startup_upgrade(engine), 20)
            await transaction.rollback()
        async with engine.connect() as conn:
            assert (await column_type(conn, 'payroll_entries', 'amount'))[0] == 'double precision'
            assert 'ix_audit_logs_created_at' not in await index_names(conn)
        await startup_upgrade(engine)
        async with engine.connect() as conn:
            assert await column_type(conn, 'payroll_entries', 'amount') == ('numeric', 14, 2)
            assert 'ix_audit_logs_created_at' in await index_names(conn)
        print('PASS PostgreSQL: money columns NUMERIC(14,2) with exact values (0.1+0.2 -> 0.30), ORM still float, '
              f'{len(PERFORMANCE_INDEXES)} list indexes, repeat run unchanged, locked table postponed without failing startup')
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
        await blocker.dispose()


if __name__ == '__main__':
    asyncio.run(main())
