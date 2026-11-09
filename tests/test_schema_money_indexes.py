"""Startup upgrade adds the list indexes to an older SQLite database (PostgreSQL:
tests/money_indexes_postgres_check.py also covers the exact money columns)."""
import asyncio

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import Base
from app.models import MONEY
from app.schema_updates import MONEY_COLUMNS, PERFORMANCE_INDEXES, upgrade


def test_money_columns_are_exact_in_the_models():
    for table, column in MONEY_COLUMNS:
        assert Base.metadata.tables[table].c[column].type is MONEY, (table, column)
    assert (MONEY.precision, MONEY.scale, MONEY.asdecimal) == (14, 2, False)
    declared = {index.name for table in Base.metadata.tables.values() for index in table.indexes}
    assert set(PERFORMANCE_INDEXES) <= declared  # fresh databases get them from create_all


def test_upgrade_adds_list_indexes_once():
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for name in PERFORMANCE_INDEXES:
                await conn.execute(text(f'DROP INDEX {name}'))
            await upgrade(conn)
            await upgrade(conn)  # a repeated start is harmless
            names = await conn.run_sync(lambda sync: {
                index['name'] for table in inspect(sync).get_table_names()
                for index in inspect(sync).get_indexes(table)})
        await engine.dispose()
        return names

    assert set(PERFORMANCE_INDEXES) <= asyncio.run(run())
