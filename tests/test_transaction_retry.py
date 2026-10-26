"""Deadlocks roll back complete writes before a bounded, safe retry."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from test_insights import fixture

from app import attendance_fines, main
from app.models import Notification, PayrollEntry, Setting
from app.push_notifications import queue_notice
from app.services import operations
from app.transaction_retry import retry_transaction, transaction_sqlstate


def database_error(state):
    original = RuntimeError('Disposable transaction conflict')
    original.sqlstate = state
    return DBAPIError('CI fixture statement', {}, original)


@pytest.mark.parametrize('state', ['40P01', '40001'])
def test_transaction_conflicts_retry_with_bound_and_preserve_last_error(state):
    async def run():
        failure = database_error(state)
        operation = AsyncMock(side_effect=[failure, failure, 'committed'])
        assert await retry_transaction(operation, delay=0) == 'committed'
        assert operation.await_count == 3
        exhausted = AsyncMock(side_effect=failure)
        with pytest.raises(DBAPIError) as result:
            await retry_transaction(exhausted, delay=0)
        assert result.value is failure and exhausted.await_count == 3
    asyncio.run(run())


def test_unrelated_errors_are_not_retried_or_classified_by_message():
    async def run():
        for error in (database_error('23505'), RuntimeError('deadlock detected')):
            operation = AsyncMock(side_effect=error)
            with pytest.raises(type(error)):
                await retry_transaction(operation, delay=0)
            assert operation.await_count == 1
        wrapped = RuntimeError('adapter')
        wrapped.__cause__ = database_error('40P01')
        assert transaction_sqlstate(wrapped) == '40P01'
    asyncio.run(run())


@pytest.mark.parametrize('channel,expected_attempts', [('app', 2), ('telegram', 1)])
def test_background_retry_uses_new_session_and_rolls_back_payroll_and_notice(monkeypatch, channel, expected_attempts):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', channel)

    async def run():
        engine, factory, _ = await fixture()
        sessions = []
        cycles = 0
        async def work(session, bot):
            nonlocal cycles
            cycles += 1
            sessions.append(session)
            if cycles > expected_attempts:
                raise asyncio.CancelledError
            session.add(PayrollEntry(user_id=2, kind='CI rollback probe', amount=-500, period='CI'))
            await queue_notice(session, 1, kind='attendance_fine', event_key='ci-retried-fine', message='CI only')
            session.add(Setting(key='ci-retried-fine', value='complete'))
            await session.flush()
            if cycles == 1:
                raise database_error('40P01')
            return {'reminders': 1}
        monkeypatch.setattr(main, 'Session', factory)
        monkeypatch.setattr(main, 'config', SimpleNamespace(admin_ids=[]))
        monkeypatch.setattr(operations, 'run_operations_once', work)
        monkeypatch.setattr(operations, 'maybe_send_daily_backup', AsyncMock(return_value=0))
        try:
            with pytest.raises(asyncio.CancelledError):
                await main.operations_loop(SimpleNamespace(send_message=AsyncMock()), interval=0)
            assert sessions[0] is not sessions[1]
            async with factory() as session:
                expected = int(channel == 'app')
                assert await session.scalar(select(func.count(PayrollEntry.id))) == expected
                assert await session.scalar(select(func.count(Notification.id)).where(
                    Notification.event_key == 'ci-retried-fine')) == expected
                assert bool(await session.get(Setting, 'ci-retried-fine')) == bool(expected)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_fine_sweep_retries_same_cycle_after_complete_rollback(monkeypatch):
    async def run():
        engine, factory, api = await fixture()
        moments = []
        async def sweep(_api, now):
            moments.append(now)
            if len(moments) > 2:
                raise asyncio.CancelledError
            async with factory() as session:
                session.add(PayrollEntry(user_id=2, kind='CI fine retry', amount=-500, period='CI'))
                await queue_notice(session, 1, kind='attendance_fine', event_key='ci-fine-loop', message='CI only')
                await session.flush()
                if len(moments) == 1:
                    raise database_error('40P01')
                await session.commit()
        async def no_wait(_delay):
            return None
        monkeypatch.setattr(attendance_fines, 'sweep', sweep)
        monkeypatch.setattr(attendance_fines.asyncio, 'sleep', no_wait)
        try:
            with pytest.raises(asyncio.CancelledError):
                await attendance_fines.fine_loop(api)
            assert moments[0] == moments[1]
            async with factory() as session:
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                assert await session.scalar(select(func.sum(PayrollEntry.amount))) == -500
                assert await session.scalar(select(func.count(Notification.id)).where(
                    Notification.event_key == 'ci-fine-loop')) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
