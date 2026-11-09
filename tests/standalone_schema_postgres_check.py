"""The reviewed 3.63 SQL brings a 3.62 standalone database to the 3.63 models.

The standalone server (photo-boss.ru) never changes its schema at startup and
refuses to start while a model column is missing. Builds a reference schema
from the current models and a copy turned into the 3.62 schema (no 3.63
columns or list indexes, double precision money), applies
migration/3.63-standalone-schema.sql to the copy as one script, as the server
operator does, and checks: the readiness schema gate passes, columns, foreign
keys and indexes match the reference, money is exact, and a repeat run
changes nothing. Disposable schemas only; no credentials or provider calls.
"""
import asyncio
import datetime
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import Base
from app.models import Booking, Client, Hotel, Package, Photo, Sale, Shooting, User
from app.schema_updates import MONEY_COLUMNS, PERFORMANCE_INDEXES

SQL = Path(__file__).resolve().parents[1] / 'migration' / '3.63-standalone-schema.sql'
NEW_COLUMNS = {'photos': ('original_name', 'source_sha256'),
               'bookings': ('no_sale_reason', 'no_sale_note', 'no_sale_at', 'no_sale_by_id')}
INDEX_TABLES = {index.name: table.name for table in Base.metadata.tables.values() for index in table.indexes}
NEW_INDEXES = ('ix_photos_source_sha256', *PERFORMANCE_INDEXES)
TABLES = sorted({table for table, _ in MONEY_COLUMNS} | set(NEW_COLUMNS)
                | {INDEX_TABLES[name] for name in NEW_INDEXES})


def readiness_gate(conn):
    """The column check of app.standalone_main.verify_readiness."""
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())
    return all(table.name in existing and set(table.columns.keys())
               <= {column['name'] for column in inspector.get_columns(table.name)}
               for table in Base.metadata.sorted_tables)


async def snapshot(dsn, schema):
    con = await asyncpg.connect(dsn, server_settings={'search_path': schema})
    try:
        columns = {tuple(row) for row in await con.fetch(
            'SELECT table_name,column_name,data_type,character_maximum_length,numeric_precision,'
            'numeric_scale,is_nullable,column_default FROM information_schema.columns '
            'WHERE table_schema=$1 AND table_name=ANY($2::text[])', schema, TABLES)}
        keys = {tuple(row) for row in await con.fetch(
            "SELECT cl.relname,c.conname,pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class cl ON cl.oid=c.conrelid JOIN pg_namespace n ON n.oid=cl.relnamespace "
            "WHERE n.nspname=$1 AND c.contype='f' AND cl.relname=ANY($2::text[])", schema, TABLES)}
        indexes = {(row['indexname'], row['indexdef'].replace(f'{schema}.', '')) for row in await con.fetch(
            'SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=$1 AND tablename=ANY($2::text[])',
            schema, TABLES)}
        money = [tuple(row) for row in await con.fetch('SELECT id,amount,commission FROM sales ORDER BY id')]
        return columns, keys, indexes, money
    finally:
        await con.close()


async def apply_sql(dsn, schema):
    # One simple-protocol script, like psql -f: BEGIN ... COMMIT inside the file.
    con = await asyncpg.connect(dsn, server_settings={'search_path': schema})
    try:
        await con.execute(SQL.read_text())
    finally:
        await con.close()


async def seed(engine):
    async with AsyncSession(engine) as session:
        session.add_all([User(id=1, tg_id=1, name='Synthetic Owner'), Client(id=1, name='Synthetic Guest'),
                         Hotel(id=1, name='Synthetic Hotel'), Package(id=1, name='Synthetic', price_per_photo=400)])
        await session.flush()
        session.add(Booking(id=1, hotel_id=1, client_id=1, package_id=1, room='1', manager_id=1, deposit=1000,
                            shoot_date=datetime.date(2026, 9, 1), shoot_time=datetime.time(12)))
        await session.flush()
        session.add(Shooting(id=1, booking_id=1))
        session.add_all([Sale(id=1, booking_id=1, created_by_id=1, credited_user_id=1, sold_photos=1,
                              amount=300, commission=30),
                         Sale(id=2, booking_id=1, created_by_id=1, credited_user_id=1, sold_photos=3,
                              amount=1200.0, commission=132.0)])
        await session.flush()
        session.add(Photo(id=1, shooting_id=1, file_id='synthetic-file-1', original_name='DSC_0001.jpg'))
        await session.commit()


async def make_legacy(engine):
    """Turn the fresh copy into the schema the 3.62 server runs on."""
    async with engine.begin() as conn:
        for table, columns in NEW_COLUMNS.items():
            for column in columns:
                await conn.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
        for name in NEW_INDEXES:
            await conn.execute(text(f'DROP INDEX IF EXISTS {name}'))
        for table, column in MONEY_COLUMNS:
            await conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN {column} TYPE DOUBLE PRECISION'))
        # Classic binary leftovers: 0.1 + 0.2 and an unrounded percentage.
        await conn.execute(text('UPDATE sales SET amount=0.1::float8+0.2::float8, commission=1234.5678 WHERE id=1'))


async def main():
    url = os.environ['UPDATE_POSTGRES_TEST_URL']
    dsn = url.replace('postgresql+asyncpg://', 'postgresql://', 1)
    reference, legacy = 'pb_ref_' + uuid4().hex, 'pb_362_' + uuid4().hex
    engines = {schema: create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
               for schema in (reference, legacy)}
    try:
        for schema, engine in engines.items():
            async with engine.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.run_sync(Base.metadata.create_all)
        await seed(engines[legacy])
        await make_legacy(engines[legacy])
        async with engines[legacy].connect() as conn:
            assert not await conn.run_sync(readiness_gate), 'the 3.62 schema must fail the 3.63 gate'

        await apply_sql(dsn, legacy)
        async with engines[legacy].connect() as conn:
            assert await conn.run_sync(readiness_gate), 'the 3.63 server would refuse to start'
        columns, keys, indexes, money = await snapshot(dsn, legacy)
        ref_columns, ref_keys, ref_indexes, _ = await snapshot(dsn, reference)
        assert columns == ref_columns, sorted(columns ^ ref_columns)
        assert keys == ref_keys, sorted(keys ^ ref_keys)
        assert indexes == ref_indexes, sorted(indexes ^ ref_indexes)
        assert set(NEW_INDEXES) <= {name for name, _ in indexes}
        assert money == [(1, Decimal('0.30'), Decimal('1234.57')), (2, Decimal('1200.00'), Decimal('132.00'))], money

        await apply_sql(dsn, legacy)
        assert await snapshot(dsn, legacy) == (columns, keys, indexes, money), 'a repeat run changed the schema'
        print('standalone 3.63 schema SQL: gate, columns, keys, indexes, exact money, repeat run ok')
    finally:
        for engine in engines.values():
            await engine.dispose()
        con = await asyncpg.connect(dsn)
        try:
            for schema in engines:
                await con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await con.close()


if __name__ == '__main__':
    asyncio.run(main())
