"""Stale Telegram flows and selected worker rows cannot cross a reset."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import development, main, operational_bot
from app.miniapp_security import AccessError
from app.services import photo_storage


class State:
    def __init__(self, state=None, data=None):
        self.state, self.data = state, data or {}

    async def get_state(self):
        return self.state

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state, self.data = None, {}

    async def update_data(self, **values):
        self.data.update(values)


def event(text=None, callback=None):
    return SimpleNamespace(text=text, data=callback, contact=None, answer=AsyncMock())


def guard_fixture(monkeypatch, *, generation='new', maintenance=False):
    lease = SimpleNamespace(active=False, entered=0)

    @asynccontextmanager
    async def operation_lease(_engine):
        lease.entered += 1
        if maintenance:
            raise AccessError('Обновляются рабочие данные.', 503)
        lease.active = True
        try:
            yield object()
        finally:
            lease.active = False

    monkeypatch.setattr(operational_bot, 'operation_lease', operation_lease)
    monkeypatch.setattr(operational_bot, 'current_generation', AsyncMock(return_value=generation))
    return lease, operational_bot.OperationalBotMiddleware(object())


@pytest.mark.parametrize('saved', [{}, {'_operations_generation': 'old'}])
def test_stale_redis_flow_is_cleared_and_pending_payload_is_rejected(monkeypatch, saved):
    async def run():
        lease, guard = guard_fixture(monkeypatch)
        state = State('S:selected', saved | {'draft_id': 10})
        message = event(callback='sale:finalize:10')
        handler = AsyncMock()
        await guard(handler, message, {'state': state, 'raw_state': 'S:selected'})
        handler.assert_not_awaited()
        assert state.state is None and state.data == {}
        message.answer.assert_awaited_once_with(operational_bot.RESET_MESSAGE)
        assert lease.entered == 1 and not lease.active
    asyncio.run(run())


def test_fresh_menu_can_restart_stale_flow_and_new_state_is_stamped(monkeypatch):
    async def run():
        lease, guard = guard_fixture(monkeypatch)
        state = State('BookingFlow:room', {'_operations_generation': 'old', 'room': 'stale'})
        data = {'state': state, 'raw_state': state.state}

        async def handler(_event, data):
            assert lease.active and data['raw_state'] is None
            assert state.state is None and state.data == {}
            state.state = 'BookingFlow:hotel'
            return 'fresh'

        assert await guard(handler, event(text='➕ Новая запись'), data) == 'fresh'
        assert state.data == {'_operations_generation': 'new'}
    asyncio.run(run())


def test_valid_flow_stays_leased_and_restamps_state_after_handler_clear(monkeypatch):
    async def run():
        lease, guard = guard_fixture(monkeypatch)
        state = State('ShiftFlow:location', {'_operations_generation': 'new'})

        async def handler(_event, _data):
            assert lease.active
            await state.clear()
            state.state = 'ShiftFlow:photo'

        await guard(handler, event(), {'state': state})
        assert state.data == {'_operations_generation': 'new'}
        assert not lease.active
    asyncio.run(run())


@pytest.mark.parametrize('message', [event(text='📚 Академия'), event(callback='academy:lesson:1'),
                                    event(callback='training_review:10'), event(callback='web-login:key'),
                                    event(text='/start login_token'), event()])
def test_academy_auth_and_unrelated_photos_are_not_blocked(monkeypatch, message):
    async def run():
        lease, guard = guard_fixture(monkeypatch, maintenance=True)
        state = State()
        handler = AsyncMock(return_value='unaffected')
        assert await guard(handler, message, {'state': state}) == 'unaffected'
        assert lease.entered == 0
    asyncio.run(run())


def test_maintenance_declines_operation_without_destroying_current_flow(monkeypatch):
    async def run():
        _, guard = guard_fixture(monkeypatch, maintenance=True)
        state = State('ReceiptFlow:photo', {'_operations_generation': 'new'})
        message, handler = event(), AsyncMock()
        await guard(handler, message, {'state': state})
        handler.assert_not_awaited()
        assert state.state == 'ReceiptFlow:photo'
        message.answer.assert_awaited_once_with('Обновляются рабочие данные.')
    asyncio.run(run())


@pytest.mark.parametrize('worker', ['operations', 'storage', 'development'])
def test_workers_skip_maintenance_without_alerts_or_holding_lease_while_sleeping(monkeypatch, worker):
    async def run():
        entered = 0

        @asynccontextmanager
        async def paused(_engine):
            nonlocal entered
            entered += 1
            raise AccessError('Обновляются рабочие данные.', 503)
            yield  # pragma: no cover - async-context-manager protocol

        async def stop(_delay):
            raise asyncio.CancelledError

        class Session:
            bind = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

        bot = SimpleNamespace(send_message=AsyncMock())
        fake_asyncio = SimpleNamespace(sleep=stop, CancelledError=asyncio.CancelledError,
                                       timeout=asyncio.timeout)
        if worker == 'operations':
            from app.services import operations
            job, backup = AsyncMock(), AsyncMock()
            monkeypatch.setattr(main, 'Session', Session)
            monkeypatch.setattr(main, 'operation_lease', paused)
            monkeypatch.setattr(main, 'asyncio', fake_asyncio)
            monkeypatch.setattr(operations, 'run_operations_once', job)
            monkeypatch.setattr(operations, 'maybe_send_daily_backup', backup)
            call = main.operations_loop(bot)
        elif worker == 'storage':
            job, backup = AsyncMock(), AsyncMock()
            monkeypatch.setattr(photo_storage, 'Session', Session)
            monkeypatch.setattr(photo_storage, 'operation_lease', paused)
            monkeypatch.setattr(photo_storage, 'asyncio', fake_asyncio)
            monkeypatch.setattr(photo_storage, 'sync_one', job)
            call = photo_storage.storage_loop(bot, object())
        else:
            job, backup = AsyncMock(), AsyncMock()
            monkeypatch.setattr(development, 'operation_lease', paused)
            monkeypatch.setattr(development, 'asyncio', fake_asyncio)
            monkeypatch.setattr(development, 'async_sessionmaker', lambda *_args, **_kwargs: Session)
            monkeypatch.setattr(development, 'queue_reviews', job)
            monkeypatch.setattr(development, 'review_one', backup)
            call = development.development_loop(object(), bot)
        with pytest.raises(asyncio.CancelledError):
            await call
        assert entered >= 1
        job.assert_not_awaited()
        backup.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize('worker', ['operations', 'storage', 'development'])
def test_worker_selected_rows_are_processed_under_lease_and_sleep_is_outside_it(monkeypatch, worker):
    async def run():
        active = False
        calls = []

        @asynccontextmanager
        async def lease(_engine):
            nonlocal active
            assert not active
            active = True
            try:
                yield object()
            finally:
                active = False

        async def job(*_args, **_kwargs):
            assert active
            calls.append('processed')
            return {} if worker == 'operations' else False

        async def backup(*_args, **_kwargs):
            assert active
            calls.append('second')
            return 0

        async def stop(_delay):
            assert not active
            raise asyncio.CancelledError

        class Session:
            bind = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def commit(self):
                assert active

        fake_asyncio = SimpleNamespace(sleep=stop, CancelledError=asyncio.CancelledError,
                                       timeout=asyncio.timeout)
        bot = SimpleNamespace(send_message=AsyncMock())
        if worker == 'operations':
            from app import delivery_bot
            from app.delivery import SERVICES
            from app.services import operations
            bot.token = 'test-guard-delivery'
            monkeypatch.setitem(SERVICES, bot.token, SimpleNamespace(api=SimpleNamespace(engine=object())))
            monkeypatch.setattr(main, 'Session', Session)
            monkeypatch.setattr(main, 'operation_lease', lease)
            monkeypatch.setattr(main, 'asyncio', fake_asyncio)
            monkeypatch.setattr(operations, 'run_operations_once', job)
            monkeypatch.setattr(operations, 'maybe_send_daily_backup', backup)
            monkeypatch.setattr(delivery_bot, 'run_guest_messages', backup)
            call = main.operations_loop(bot)
        elif worker == 'storage':
            from app import photo_edits
            monkeypatch.setattr(photo_storage, 'Session', Session)
            monkeypatch.setattr(photo_storage, 'operation_lease', lease)
            monkeypatch.setattr(photo_storage, 'asyncio', fake_asyncio)
            monkeypatch.setattr(photo_storage, 'sync_one', job)
            monkeypatch.setattr(photo_edits, 'process_one', backup)
            call = photo_storage.storage_loop(bot, object())
        else:
            from app.services import shooting_media_audit
            monkeypatch.setattr(development, 'operation_lease', lease)
            monkeypatch.setattr(development, 'asyncio', fake_asyncio)
            monkeypatch.setattr(development, 'async_sessionmaker', lambda *_args, **_kwargs: Session)
            monkeypatch.setattr(development, 'queue_reviews', job)
            monkeypatch.setattr(shooting_media_audit, 'audit_next_files', backup)
            monkeypatch.setattr(development, 'review_one', backup)
            call = development.development_loop(object(), bot)
        with pytest.raises(asyncio.CancelledError):
            await call
        assert calls == (['processed', 'second', 'second'] if worker in {'operations', 'development'} else ['processed', 'second'])
        assert not active

    asyncio.run(run())
