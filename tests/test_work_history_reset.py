import asyncio
import json

import pytest
from sqlalchemy import text
from test_operational_reset import fixture

from app.operational_reset import ResetError
from app.work_history_reset import REQUEST_ID, reset_old_work


def test_reset_archives_all_work_preserves_attendance_and_new_work_afterwards():
    async def run():
        engine = await fixture()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM settings WHERE key='operations:maintenance'"))
                await conn.execute(text('''INSERT INTO bookings(id,hotel_id,client_id,room,guest_count,deposit,
                    shoot_date,shoot_time,package_id,manager_id,status,created_at)
                    SELECT 100,hotel_id,client_id,room,guest_count,deposit,shoot_date,shoot_time,
                    package_id,manager_id,'NEW',CURRENT_TIMESTAMP FROM bookings WHERE id=10'''))
                kept = {name: (await conn.execute(text(f'SELECT * FROM {name}'))).all()
                        for name in ('users', 'user_roles', 'hotels', 'packages', 'cash_movements',
                            'shift_check_ins', 'shift_check_outs', 'work_breaks', 'shifts',
                            'training_assignments', 'training_submissions', 'work_chat_messages', 'operation_requests')}
            result = await reset_old_work(engine, {8000000001})
            assert result['bookings'] == 2 and result['shootings'] == result['sales'] == 1
            assert not result['replayed']
            async with engine.connect() as conn:
                for name, rows in kept.items():
                    assert (await conn.execute(text(f'SELECT * FROM {name}'))).all() == rows
                assert list(await conn.scalars(text('SELECT id FROM bookings'))) == []
                for name in ('shootings', 'sales', 'sale_drafts', 'photos', 'receipts', 'sale_draft_photos'):
                    assert await conn.scalar(text(f'SELECT count(*) FROM {name}')) == 0
                assert await conn.scalar(text('SELECT recommendation FROM academy_reviews WHERE id=25')) == 'Keep this feedback'
                assert await conn.scalar(text('SELECT photo_id FROM academy_reviews WHERE id=25')) is None
                saved = json.loads(await conn.scalar(text('SELECT payload FROM operational_reset_archives WHERE request_id=:id'), {'id': REQUEST_ID}))
                assert saved['deleted']['photos']['rows'] and saved['deleted']['sales']['rows']
                assert await conn.scalar(text("SELECT value FROM settings WHERE key='operations:generation'"))
                assert not (await conn.exec_driver_sql('PRAGMA foreign_key_check')).all()
            async with engine.begin() as conn:
                await conn.execute(text('''INSERT INTO bookings(id,hotel_id,client_id,room,guest_count,deposit,
                    shoot_date,shoot_time,package_id,manager_id,status,created_at)
                    VALUES (100,1,1,'New',1,0,'2026-10-21','15:00',1,1,'NEW',CURRENT_TIMESTAMP)'''))
            assert (await reset_old_work(engine, set()))['replayed']
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT count(*) FROM bookings WHERE id=100')) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_reset_requires_configured_active_owner():
    async def run():
        engine = await fixture()
        try:
            with pytest.raises(ResetError, match='owner'):
                await reset_old_work(engine, set())
            async with engine.connect() as conn:
                assert not await conn.scalar(text('SELECT count(*) FROM operational_reset_archives'))
        finally:
            await engine.dispose()
    asyncio.run(run())
