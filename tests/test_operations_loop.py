"""Background failures must not block independent jobs or spam the owner."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_insights import fixture

from app import main
from app.models import Setting
from app.services import operations


@pytest.mark.parametrize('failed_job', ['operations', 'backup'])
def test_background_jobs_commit_independently_and_throttle_repeated_alerts(monkeypatch, failed_job):
    async def run():
        engine, factory, _ = await fixture()
        attempt = 0
        async def work(session, bot):
            nonlocal attempt
            attempt += 1
            if attempt == 4:
                raise asyncio.CancelledError
            session.add(Setting(key=f'work-{attempt}', value='saved'))
            await session.flush()
            if failed_job == 'operations':
                raise RuntimeError('Fixture operation failure')
            return {'reminders': 1}
        async def backup(session, bot):
            session.add(Setting(key=f'backup-{attempt}', value='saved'))
            await session.flush()
            if failed_job == 'backup':
                raise RuntimeError('Fixture export failure')
            return 1
        monkeypatch.setattr(main, 'Session', factory)
        monkeypatch.setattr(main, 'config', SimpleNamespace(admin_ids=[6000000001]))
        monkeypatch.setattr(operations, 'run_operations_once', work)
        monkeypatch.setattr(operations, 'maybe_send_daily_backup', backup)
        bot = SimpleNamespace(send_message=AsyncMock())
        try:
            with pytest.raises(asyncio.CancelledError):
                await main.operations_loop(bot, interval=0)
            async with factory() as session:
                keys = set(await session.scalars(select(Setting.key)))
            expected = 'backup' if failed_job == 'operations' else 'work'
            rejected = 'work' if failed_job == 'operations' else 'backup'
            assert {f'{expected}-{n}' for n in range(1, 4)} <= keys
            assert not any(k.startswith(rejected+'-') for k in keys)
            assert bot.send_message.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_recovered_background_job_can_report_a_new_incident(monkeypatch):
    async def run():
        engine, factory, _ = await fixture()
        error = RuntimeError('Fixture export failure')
        work = AsyncMock(side_effect=[{}, {}, {}, {}, asyncio.CancelledError()])
        backup = AsyncMock(side_effect=[error, error, 1, error])
        monkeypatch.setattr(main, 'Session', factory)
        monkeypatch.setattr(main, 'config', SimpleNamespace(admin_ids=[6000000001]))
        monkeypatch.setattr(operations, 'run_operations_once', work)
        monkeypatch.setattr(operations, 'maybe_send_daily_backup', backup)
        bot = SimpleNamespace(send_message=AsyncMock())
        try:
            with pytest.raises(asyncio.CancelledError):
                await main.operations_loop(bot, interval=0)
            assert backup.await_count == 4
            assert bot.send_message.await_count == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_unresolved_background_failure_is_reported_again_after_one_hour(monkeypatch):
    async def run():
        engine, factory, _ = await fixture()
        clock = iter([0, 300, 3599, 3600])
        monkeypatch.setattr(main, 'monotonic', lambda: next(clock))
        monkeypatch.setattr(main, 'Session', factory)
        monkeypatch.setattr(main, 'config', SimpleNamespace(admin_ids=[6000000001]))
        monkeypatch.setattr(operations, 'run_operations_once', AsyncMock(side_effect=[{}, {}, {}, {}, asyncio.CancelledError()]))
        monkeypatch.setattr(operations, 'maybe_send_daily_backup', AsyncMock(side_effect=RuntimeError('Fixture failure')))
        bot = SimpleNamespace(send_message=AsyncMock())
        try:
            with pytest.raises(asyncio.CancelledError):
                await main.operations_loop(bot, interval=0)
            assert bot.send_message.await_count == 2
        finally:
            await engine.dispose()
    asyncio.run(run())
