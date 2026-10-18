"""Deployment-triggered resets require one owner, one epoch and one archive."""
import asyncio
import json
import logging
from datetime import date, time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_operational_reset import fixture

from app.models import Booking, User
from app.operational_reset import ResetError, preview, reset
from app.operations_guard import operation_lease
from app.operations_reset_startup import parse_request, startup_reset

OWNER_TG = 8000000001
REQUEST_ID = 'deployment-reset-0001'


def request(request_id=REQUEST_ID, generation=''):
    return json.dumps({'requestId': request_id, 'expectedGeneration': generation,
                       'scope': 'all-operational-history'})


async def prepare():
    engine = await fixture()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM settings WHERE key='operations:maintenance'"))
    return engine


async def marker(engine, name='operations:maintenance'):
    async with engine.connect() as conn:
        return await conn.scalar(text('SELECT value FROM settings WHERE key=:key'), {'key': name})


@pytest.mark.parametrize('raw', ['[]', '{}', 'null', '{bad-json',
    request('short'), request('unsafe/request'),
    json.dumps({'requestId': REQUEST_ID, 'expectedGeneration': '', 'scope': 'database'}),
    json.dumps({'requestId': REQUEST_ID, 'expectedGeneration': None, 'scope': 'all-operational-history'}),
    json.dumps({'requestId': REQUEST_ID, 'expectedGeneration': '', 'scope': 'all-operational-history', 'force': True}),
])
def test_deployment_request_has_strict_scope_and_fields(raw):
    with pytest.raises(ResetError):
        parse_request(raw)


def test_no_request_is_read_only_and_zero_preview_is_logged_without_private_rows(caplog):
    async def run():
        engine = await prepare()
        try:
            before = await preview(engine)
            assert before['total_deleted_rows'] > 0
            with caplog.at_level(logging.INFO, logger='app.operations_reset_startup'):
                assert await startup_reset(engine, '', set()) is None
            assert await preview(engine) == before
            assert 'test-secret-preserved' not in caplog.text
            assert '+79991234567' not in caplog.text
            assert await marker(engine) is None
            await reset(engine, REQUEST_ID, 1)
            caplog.clear()
            with caplog.at_level(logging.INFO, logger='app.operations_reset_startup'):
                assert await startup_reset(engine, '  ', set()) is None
            records = [r.message for r in caplog.records if r.name == 'app.operations_reset_startup']
            assert len(records) == 1 and records[0].startswith('Operations reset preview: ')
            summary = json.loads(records[0].split(': ', 1)[1])
            assert summary['total_deleted_rows'] == 0
            assert all(count == 0 for count in summary['row_counts'].values())
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_missing_initial_epoch_resets_once_and_retained_env_never_clears_new_work():
    async def run():
        engine = await prepare()
        try:
            result = await startup_reset(engine, request(), {OWNER_TG})
            assert not result['replayed']
            assert await marker(engine) is None
            assert await marker(engine, 'operations:generation') == result['generation']
            assert (await preview(engine))['total_deleted_rows'] == 0
            async with engine.begin() as conn:
                await conn.execute(Booking.__table__.insert().values(id=100, hotel_id=1, client_id=1,
                    room='New', shoot_date=date(2026, 10, 18), shoot_time=time(15), package_id=1, manager_id=1))
            # Expected old generation differs now; completed request wins on a restart.
            again = await startup_reset(engine, request(), set())
            assert again['replayed'] and again['archive_sha256'] == result['archive_sha256']
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT count(*) FROM bookings')) == 1
                assert await conn.scalar(text('SELECT count(*) FROM operational_reset_archives')) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_wrong_epoch_and_unconfigured_or_inactive_owner_never_enter_maintenance():
    async def run():
        engine = await prepare()
        try:
            before = await preview(engine)
            with pytest.raises(ResetError, match='different operational generation'):
                await startup_reset(engine, request(generation='old-generation'), {OWNER_TG})
            assert await marker(engine) is None
            for configured in (set(), {8000000002}):
                with pytest.raises(ResetError, match='configured active owner'):
                    await startup_reset(engine, request(), configured)
                assert await marker(engine) is None
            async with engine.begin() as conn:
                await conn.execute(text('UPDATE users SET active=false WHERE id=1'))
            with pytest.raises(ResetError, match='configured active owner'):
                await startup_reset(engine, request(), {OWNER_TG})
            assert await marker(engine) is None
            assert (await preview(engine))['row_counts'] == before['row_counts']
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_only_configured_active_owner_is_recorded_in_archive():
    async def run():
        engine = await prepare()
        try:
            async with engine.begin() as conn:
                await conn.execute(User.__table__.insert().values(id=3, tg_id=333, name='Configured owner', active=True))
                await conn.execute(text("INSERT INTO user_roles(user_id,role) VALUES (3,'OWNER')"))
            await startup_reset(engine, request(), {333})
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT owner_id FROM operational_reset_archives')) == 3
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_reset_failure_keeps_maintenance_and_blocks_operations_until_retried():
    async def run():
        engine = await prepare()
        try:
            before = await preview(engine)
            async with engine.begin() as conn:
                await conn.exec_driver_sql("""CREATE TRIGGER reject_booking_delete BEFORE DELETE ON bookings
                    BEGIN SELECT RAISE(ABORT, 'fixture forced failure'); END""")
            with pytest.raises(IntegrityError, match='fixture forced failure'):
                await startup_reset(engine, request(), {OWNER_TG})
            assert await marker(engine) == REQUEST_ID
            assert (await preview(engine))['row_counts'] == before['row_counts']
            with pytest.raises(Exception) as error:
                async with operation_lease(engine):
                    pytest.fail('An operational handler entered during failed reset maintenance')
            assert error.value.status == 503
            async with engine.begin() as conn:
                assert await conn.scalar(text('SELECT count(*) FROM operational_reset_archives')) == 0
                await conn.exec_driver_sql('DROP TRIGGER reject_booking_delete')
            assert not (await startup_reset(engine, request(), {OWNER_TG}))['replayed']
            assert await marker(engine) is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_two_deployment_request_ids_cannot_claim_one_maintenance_window(monkeypatch):
    async def run():
        engine = await prepare()
        entered, release = asyncio.Event(), asyncio.Event()
        actual_preview = preview

        async def held_preview(target):
            entered.set()
            await release.wait()
            return await actual_preview(target)

        monkeypatch.setattr('app.operations_reset_startup.preview', held_preview)
        first = asyncio.create_task(startup_reset(engine, request(), {OWNER_TG}))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            with pytest.raises(ResetError, match='Another operational maintenance'):
                await startup_reset(engine, request('deployment-reset-0002'), {OWNER_TG})
            assert await marker(engine) == REQUEST_ID
            release.set()
            result = await first
            assert result['request_id'] == REQUEST_ID
            assert (await actual_preview(engine))['total_deleted_rows'] == 0
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT count(*) FROM operational_reset_archives')) == 1
        finally:
            release.set()
            if not first.done():
                await first
            await engine.dispose()
    asyncio.run(run())
