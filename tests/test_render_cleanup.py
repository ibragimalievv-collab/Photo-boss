"""Failed background jobs cannot prevent shutdown of the remaining resources."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.render_main import on_cleanup


def resources():
    return {
        'ready': True,
        'dispatcher': SimpleNamespace(storage=SimpleNamespace(close=AsyncMock())),
        'bot': SimpleNamespace(session=SimpleNamespace(close=AsyncMock())),
    }


def test_failed_worker_does_not_abort_render_cleanup():
    async def run():
        app = resources()

        async def failed():
            raise RuntimeError('worker already failed')

        async def waiting():
            await asyncio.Event().wait()

        app['owner_telegram_task'] = asyncio.create_task(failed())
        app['operations_task'] = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        dispose, leases = AsyncMock(), AsyncMock()
        with patch('app.render_main.clear_ready_file'), patch('app.render_main.engine', SimpleNamespace(dispose=dispose)), patch('app.operations_guard.close_operation_leases', leases):
            await on_cleanup(app)
        assert app['ready'] is False
        assert app['operations_task'].cancelled()
        app['dispatcher'].storage.close.assert_awaited_once()
        app['bot'].session.close.assert_awaited_once()
        leases.assert_awaited_once()
        dispose.assert_awaited_once()

    asyncio.run(run())


def test_storage_close_failure_still_releases_http_and_database():
    async def run():
        app = resources()
        app['dispatcher'].storage.close.side_effect = RuntimeError('storage close failed')
        dispose, leases = AsyncMock(), AsyncMock()
        with patch('app.render_main.clear_ready_file'), patch('app.render_main.engine', SimpleNamespace(dispose=dispose)), patch('app.operations_guard.close_operation_leases', leases), pytest.raises(RuntimeError, match='storage close failed'):
            await on_cleanup(app)
        app['bot'].session.close.assert_awaited_once()
        leases.assert_awaited_once()
        dispose.assert_awaited_once()

    asyncio.run(run())
