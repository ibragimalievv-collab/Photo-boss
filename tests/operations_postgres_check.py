"""Real operations/attendance lock ordering and deadlock retries in CI only."""
import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import test_insights
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import booking_control
from app.db import user_lock_clause
from app.models import (
    Notification,
    PayrollEntry,
    Sale,
    Shift,
    ShiftCheckIn,
    User,
    UserRole,
)
from app.push_notifications import queue_notice
from app.services.operations import run_operations_once
from app.transaction_retry import retry_transaction

NOW = datetime(2026, 9, 21, 8, tzinfo=UTC)


async def main():
    # Deliberately no DATABASE_URL fallback: never use the production database.
    schema = 'pb_operations_test_' + uuid4().hex
    engine = create_async_engine(os.environ['UPDATE_POSTGRES_TEST_URL'],
        connect_args={'server_settings': {'search_path': schema}})
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        with patch.object(test_insights, 'create_async_engine', return_value=engine), patch.dict(os.environ, {
                'PHOTO_BOSS_NOTIFICATION_CHANNEL': 'app'}):
            _, factory, _api = await test_insights.fixture()
            async with factory() as session:
                session.add(User(id=3, tg_id=3, name='CI Manager'))
                await session.flush()
                session.add_all([
                    UserRole(user_id=3, role='MANAGER'),
                    Shift(user_id=3, hotel_id=1, start_at=NOW.replace(tzinfo=None)-timedelta(hours=2),
                        end_at=NOW.replace(tzinfo=None)+timedelta(hours=3)),
                    ShiftCheckIn(user_id=3, shift_date=NOW.date(), status='STARTED',
                        started_at=NOW.replace(tzinfo=None)-timedelta(hours=2)),
                ])
                await session.commit()
                await run_operations_once(session, SimpleNamespace(send_message=AsyncMock()), NOW)
                target = await session.scalar(select(Notification.id).where(
                    Notification.user_id == 1, Notification.event_key == 'sale:1:commission'))
                assert target is not None
                original_count = await session.scalar(select(func.count(Notification.id)))
            attendance_locked, background_notices_locked = asyncio.Event(), asyncio.Event()
            original_activity = booking_control.activity
            async def observed_activity(session, **kwargs):
                data = await original_activity(session, **kwargs)
                if session.info.get('qa_operations'):
                    background_notices_locked.set()
                    await asyncio.sleep(0)
                return data

            async def attendance():
                async with engine.begin() as conn:
                    await conn.execute(text('SELECT id FROM users WHERE id=3' + user_lock_clause(conn)))
                    attendance_locked.set()
                    await background_notices_locked.wait()
                    # Existing owner notices used to be held before operations
                    # waited for this employee. That inversion deadlocked here.
                    await conn.execute(text('UPDATE notifications SET text=text WHERE id=:id'), {'id': target})
                    await conn.execute(text('''INSERT INTO notifications(user_id,text,event_key,kind,sent)
                        VALUES (1,'CI only','ci-attendance-overlap','attendance',FALSE)'''))

            async def operations():
                await attendance_locked.wait()
                async with factory() as session:
                    session.info['qa_operations'] = True
                    await run_operations_once(session, SimpleNamespace(send_message=AsyncMock()), NOW)
            with patch.object(booking_control, 'activity', observed_activity):
                await asyncio.wait_for(asyncio.gather(attendance(), operations()), timeout=10)
            async with factory() as session:
                assert await session.scalar(select(func.count(Notification.id))) == original_count + 1
                assert await session.scalar(select(func.count(Notification.id)).where(
                    Notification.event_key == 'ci-attendance-overlap')) == 1
                sale = await session.get(Sale, 1)
                assert sale.amount == 800 and sale.commission == 120
            print('PASS PostgreSQL: operations/attendance overlap completes; notices deduplicate; sale unchanged')

            # Force a real PostgreSQL deadlock. The victim must roll back its
            # payroll insert and start a fresh transaction before retrying.
            ready = [asyncio.Event(), asyncio.Event()]
            attempts = [0, 0]

            async def conflicting(index):
                async def transaction():
                    attempts[index] += 1
                    async with factory() as session:
                        session.add(PayrollEntry(user_id=2, kind=f'CI deadlock {index}', amount=-500, period='CI'))
                        await session.flush()
                        await session.execute(text('SELECT id FROM users WHERE id=:uid FOR NO KEY UPDATE'),
                            {'uid': index + 1})
                        if attempts[index] == 1:
                            ready[index].set()
                            await ready[1-index].wait()
                        await session.execute(text('SELECT id FROM users WHERE id=:uid FOR NO KEY UPDATE'),
                            {'uid': 2 - index})
                        await queue_notice(session, 1, kind='attendance_fine',
                            event_key=f'ci-deadlock-retry:{index}', message='CI only')
                        await session.commit()
                await retry_transaction(transaction)
            await asyncio.wait_for(asyncio.gather(conflicting(0), conflicting(1)), timeout=15)
            assert sorted(attempts) == [1, 2], attempts
            async with factory() as session:
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 2
                assert await session.scalar(select(func.sum(PayrollEntry.amount))) == -1000
                assert await session.scalar(select(func.count(Notification.id)).where(
                    Notification.event_key.like('ci-deadlock-retry:%'))) == 2
            print('PASS PostgreSQL: genuine deadlock retries once; payroll and notices committed exactly once')
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
