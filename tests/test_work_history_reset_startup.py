"""A Render identity is never authorization to erase business history."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from test_operational_reset import fixture

from app.operational_reset import ResetError
from app.work_history_reset import PRODUCTION_SERVICE, REQUEST_ID


def test_render_startup_preserves_business_rows_without_explicit_reset(monkeypatch):
    from app import render_main

    class StartupReachedTelegram(RuntimeError):
        pass

    async def run():
        engine = await fixture()
        try:
            monkeypatch.setenv('RENDER_SERVICE_ID', PRODUCTION_SERVICE)
            monkeypatch.delenv('PHOTO_BOSS_WORK_HISTORY_RESET_REQUEST', raising=False)
            monkeypatch.delenv('PHOTO_BOSS_SALES_FINES_RESET_REQUEST', raising=False)
            monkeypatch.setattr(render_main, 'engine', engine)
            monkeypatch.setattr(render_main, 'clear_ready_file', lambda: None)
            monkeypatch.setattr(render_main, 'config', SimpleNamespace(validate=lambda: None, admin_ids={8000000001}))
            monkeypatch.setattr(render_main, 'wait_for_database', AsyncMock())
            monkeypatch.setattr(render_main, 'init_db', AsyncMock())
            monkeypatch.setattr('app.operations_reset_startup.startup_reset', AsyncMock())
            monkeypatch.setattr('app.sales_reset.startup_sales_reset', AsyncMock())
            monkeypatch.setattr('app.hotel_price_defaults.apply_owner_hotel_prices', AsyncMock())
            monkeypatch.setattr('app.work_history_reset.log_booking_readiness', AsyncMock())
            monkeypatch.setattr('app.hotel_points.startup_hotel_point', AsyncMock())
            bot = SimpleNamespace(me=AsyncMock(side_effect=StartupReachedTelegram))
            async with engine.connect() as conn:
                before = {name: (await conn.execute(text(f'SELECT * FROM {name}'))).all()
                          for name in ('bookings', 'sales', 'shootings', 'photos', 'receipts', 'payroll_entries')}
            with pytest.raises(StartupReachedTelegram):
                await render_main.on_startup({'bot': bot})
            async with engine.connect() as conn:
                for name, rows in before.items():
                    assert (await conn.execute(text(f'SELECT * FROM {name}'))).all() == rows
                assert not await conn.scalar(text('SELECT count(*) FROM operational_reset_archives'))
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_work_history_startup_requires_exact_request_and_matching_generation():
    from app.work_history_reset import SCOPE, startup_work_history_reset

    async def run():
        engine = await fixture()
        request = {'requestId': REQUEST_ID, 'scope': SCOPE, 'expectedGeneration': ''}
        try:
            assert await startup_work_history_reset(engine, '', set()) is None
            assert await startup_work_history_reset(engine, '  ', set()) is None
            for raw in ['1', 'true', '[]', 'invalid', '{}',
                        json.dumps(request | {'requestId': 'other-request'}),
                        json.dumps(request | {'scope': 'all'}),
                        json.dumps(request | {'expectedGeneration': []}),
                        json.dumps(request | {'unexpected': True})]:
                with pytest.raises(ResetError):
                    await startup_work_history_reset(engine, raw, {8000000001})
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO settings(key,value) VALUES('operations:generation','current-epoch')"))
            with pytest.raises(ResetError, match='different operational generation'):
                await startup_work_history_reset(engine, json.dumps(request), {8000000001})
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT count(*) FROM bookings')) == 1
                assert not await conn.scalar(text('SELECT count(*) FROM operational_reset_archives'))
            with pytest.raises(ResetError, match='owner'):
                await startup_work_history_reset(engine, json.dumps(request | {'expectedGeneration': 'current-epoch'}), set())
            authorized = json.dumps(request | {'expectedGeneration': 'current-epoch'})
            result = await startup_work_history_reset(engine, authorized, {8000000001})
            assert not result['replayed'] and result['bookings'] == 1
            async with engine.begin() as conn:
                await conn.execute(text('''INSERT INTO bookings(id,hotel_id,client_id,room,guest_count,deposit,
                    shoot_date,shoot_time,package_id,manager_id,status,created_at)
                    VALUES (100,1,1,'New',1,0,'2026-10-23','15:00',1,1,'NEW',CURRENT_TIMESTAMP)'''))
            # A retained request cannot delete fresh work after restart, even though generation changed.
            assert (await startup_work_history_reset(engine, authorized, set()))['replayed']
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT count(*) FROM bookings WHERE id=100')) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
