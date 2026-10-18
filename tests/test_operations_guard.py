"""SQLite leases never hold readers across independent handler transactions."""
import asyncio

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.miniapp_security import AccessError
from app.operational_reset import reset
from app.operations_guard import current_generation, operation_lease, require_generation


async def file_database(path):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(path), connect_args={'timeout': .2})

    @event.listens_for(engine.sync_engine, 'connect')
    def explicit_transactions(dbapi, _):
        dbapi.isolation_level = None

    @event.listens_for(engine.sync_engine, 'begin')
    def begin(conn):
        conn.exec_driver_sql('BEGIN')

    async with engine.begin() as conn:
        await conn.exec_driver_sql('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        await conn.exec_driver_sql("INSERT INTO settings VALUES ('operations:generation','epoch-one')")
        await conn.exec_driver_sql('CREATE TABLE saved_operations(id INTEGER PRIMARY KEY)')
    return engine


def test_file_sqlite_lease_releases_reader_before_handler_commit(tmp_path):
    async def run():
        engine = await file_database(tmp_path / 'lease.db')
        try:
            async with operation_lease(engine) as lease:
                assert await current_generation(lease) == 'epoch-one'
                await require_generation(lease, 'epoch-one')
                assert engine.pool.checkedout() == 0
                async with engine.begin() as conn:
                    await conn.exec_driver_sql('INSERT INTO saved_operations VALUES (1)')
                # Generation validation must continue to use the cached marker,
                # opening no new reader while another connection commits.
                await require_generation(lease, 'epoch-one')
                assert engine.pool.checkedout() == 0
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT count(*) FROM saved_operations')) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_file_sqlite_concurrent_operational_handlers_are_serialized(tmp_path):
    async def run():
        engine = await file_database(tmp_path / 'serialized.db')
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def first():
            async with operation_lease(engine):
                order.append('first-entered')
                async with engine.begin() as conn:
                    await conn.exec_driver_sql('INSERT INTO saved_operations VALUES (1)')
                    entered.set()
                    await release.wait()
                order.append('first-committed')

        async def second():
            async with operation_lease(engine):
                order.append('second-entered')
                async with engine.begin() as conn:
                    await conn.exec_driver_sql('INSERT INTO saved_operations VALUES (2)')

        one = asyncio.create_task(first())
        two = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            two = asyncio.create_task(second())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(two), .05)
            assert order == ['first-entered']
            release.set()
            await asyncio.gather(one, two)
            assert order == ['first-entered', 'first-committed', 'second-entered']
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT count(*) FROM saved_operations')) == 2
        finally:
            release.set()
            await asyncio.gather(one, *([two] if two is not None else []), return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


def test_sqlite_reset_waits_for_live_handler_before_changing_epoch():
    from test_operational_reset import fixture

    async def run():
        engine = await fixture()
        pending = None
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM settings WHERE key='operations:maintenance'"))
            async with operation_lease(engine) as lease:
                assert await current_generation(lease) == ''
                pending = asyncio.create_task(reset(engine, 'reset-barrier-check', 1))
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(pending), .05)
                assert await current_generation(lease) == ''
            result = await asyncio.wait_for(pending, 10)
            assert not result['replayed']
            async with operation_lease(engine) as lease:
                assert await current_generation(lease) == result['generation']
            # A failed maintenance lease must release its Python lock too.
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO settings VALUES ('operations:maintenance','another-reset')"))
            for _ in range(2):
                with pytest.raises(AccessError) as error:
                    async with operation_lease(engine):
                        pytest.fail('Maintenance permitted a new handler')
                assert error.value.status == 503
        finally:
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())
