"""A reset invalidates old mutations without losing unrelated idempotency."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import test_miniapp_release as miniapp_fixtures
from aiohttp import web
from sqlalchemy import func, select, text
from test_workflow_replay import ACTOR, packet, setup
from test_workflow_services import booking_data

from app.miniapp_api import OPERATIONS_GENERATION_HEADER, operational_mutation
from app.miniapp_security import AccessError
from app.models import Booking, CashMovement, OperationRequest
from app.operations_guard import OperationsResetError


async def set_generation(engine, generation):
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO settings(key,value) VALUES ('operations:generation',:value) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
                           {'value': generation})


@pytest.mark.parametrize('path', [
    '/api/miniapp/attendance/location', '/api/miniapp/attendance/photo',
    '/api/miniapp/attendance/review', '/api/miniapp/bookings/27/photographer',
    '/api/miniapp/bookings/27/shooting/finish', '/api/miniapp/bookings/27/manage',
    '/api/miniapp/operations/sync', '/api/miniapp/operations/media',
    '/api/miniapp/sales/12/feedback-link', '/api/miniapp/photos/4/edit',
    '/api/miniapp/delivery/27', '/api/miniapp/delivery/27/photos/4/selected',
    '/api/miniapp/galleries/27/preferences', '/api/miniapp/galleries/27/purge',
    '/api/miniapp/galleries/27/photos/4/favorite', '/api/miniapp/events/scan',
    '/api/miniapp/events/12/acknowledge', '/feedback/capability',
    '/g/capability/unlock', '/g/capability/photos/4/favorite',
])
def test_operational_write_route_map(path):
    assert operational_mutation(SimpleNamespace(method='POST', path=path))
    assert not operational_mutation(SimpleNamespace(method='GET', path=path))


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/miniapp/schedule'), ('POST', '/api/miniapp/schedule/team'),
    ('DELETE', '/api/miniapp/schedule/7'), ('POST', '/api/miniapp/people'),
    ('PUT', '/api/miniapp/people/7'), ('POST', '/api/miniapp/hotels'),
    ('POST', '/api/miniapp/academy/sales-training'), ('POST', '/api/miniapp/chat/messages'),
    ('POST', '/api/miniapp/session'), ('PUT', '/api/miniapp/preferences'),
    ('POST', '/api/miniapp/galleries/defaults'), ('PUT', '/api/miniapp/booking-control/settings'),
    ('PUT', '/api/miniapp/workday/checklist'), ('PUT', '/api/miniapp/events/preferences'),
    ('POST', '/api/miniapp/cash-movements/7/void'), ('POST', '/auth/password/login'),
])
def test_preserved_write_route_map(method, path):
    assert not operational_mutation(SimpleNamespace(method=method, path=path))


def test_me_advertises_epoch_and_middleware_blocks_stale_direct_writes():
    async def run():
        fixture = miniapp_fixtures.MiniAppTests()
        await fixture.asyncSetUp()
        try:
            status, body, _ = await fixture.call('/me')
            assert status == 200 and body['operationsGeneration'] == ''
            await set_generation(fixture.engine, 'reset-epoch-1')
            status, body, _ = await fixture.call('/me')
            assert status == 200 and body['operationsGeneration'] == 'reset-epoch-1'
            for value in (None, '', 'reset-epoch-before'):
                request = miniapp_fixtures.Request('/api/miniapp/attendance/photo', 1001, 'POST', {}, None)
                if value is not None:
                    request.headers[OPERATIONS_GENERATION_HEADER] = value
                handler = AsyncMock(return_value=web.json_response({'ok': True}))
                response = await fixture.service.middleware(request, handler)
                assert response.status == 409
                assert json.loads(response.text) == {
                    'error': str(OperationsResetError('reset-epoch-1')),
                    'code': 'operations_reset', 'operationsGeneration': 'reset-epoch-1'}
                handler.assert_not_called()
            request.headers[OPERATIONS_GENERATION_HEADER] = 'reset-epoch-1'
            assert (await fixture.service.middleware(request, handler)).status == 200
            handler.assert_awaited_once()
            # Retained preferences work in a previously open tab without an epoch.
            assert (await fixture.call('/preferences', method='PUT', body={'theme': 'light'}))[0] == 200
        finally:
            await fixture.asyncTearDown()
    asyncio.run(run())


def test_operation_lease_covers_handler_and_guest_capabilities_without_staff_header():
    async def run():
        fixture = miniapp_fixtures.MiniAppTests()
        await fixture.asyncSetUp()
        held = False

        @asynccontextmanager
        async def lease(engine):
            nonlocal held
            assert not held
            async with engine.begin() as conn:
                held = True
                try:
                    yield conn
                finally:
                    held = False

        async def handler(request):
            assert held
            return web.json_response({'ok': True})

        try:
            await set_generation(fixture.engine, 'reset-epoch-1')
            with patch('app.miniapp_api.operation_lease', lease):
                for method, path in (('POST', '/api/miniapp/operations/sync'),
                                     ('POST', '/feedback/guest-capability'),
                                     ('POST', '/g/guest-capability/unlock'),
                                     ('GET', '/g/guest-capability'),
                                     ('GET', '/g/guest-capability/album.zip')):
                    request = miniapp_fixtures.Request(path, 1001, method, {}, None)
                    assert (await fixture.service.middleware(request, handler)).status == 200
                    assert not held
        finally:
            await fixture.asyncTearDown()
    asyncio.run(run())


def test_pre_reset_replay_rejected_before_idempotency_lookup_and_new_booking_works():
    async def run():
        engine, factory, work = await setup()
        try:
            old = packet('before-reset', 'booking', booking_data() | {'photographer_id': None})
            before = json.loads((await work.execute(ACTOR, old)).text)
            await set_generation(engine, 'reset-epoch-1')
            async with engine.begin() as conn:
                # Model the operational deletion while preserving the tombstone.
                await conn.execute(text('DELETE FROM shootings'))
                await conn.execute(text('DELETE FROM bookings'))
            for supplied in (None, '', 'old-epoch'):
                stale = old if supplied is None else old | {'generation': supplied}
                with pytest.raises(OperationsResetError) as error:
                    await work.execute(ACTOR, stale)
                assert error.value.generation == 'reset-epoch-1'
            async with factory() as session:
                assert await session.scalar(select(func.count(Booking.id))) == 0
                assert await session.scalar(select(func.count(OperationRequest.id))) == 1
            fresh = packet('after-reset', 'booking', booking_data() | {'photographer_id': None}) | {'generation': 'reset-epoch-1'}
            created = json.loads((await work.execute(ACTOR, fresh)).text)
            assert json.loads((await work.execute(ACTOR, fresh)).text) == created
            assert 'bookingId' in before and 'bookingId' in created
            # A generation must be a string; unrelated envelope fields stay forbidden.
            for extra in ({'generation': None}, {'generation': 42}, {'unknown': 'x'}):
                with pytest.raises(AccessError) as error:
                    await work.execute(ACTOR, fresh | extra)
                assert error.value.status == 400
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_cash_expense_legacy_hash_and_replay_survive_epoch_change():
    from test_cash_control import operation
    from test_team import OWNER, Request
    from test_workday import fixture

    async def run():
        engine, factory, work = await fixture()
        try:
            body = operation()
            first = json.loads((await work.operation(Request(OWNER, body))).text)
            await set_generation(engine, 'reset-epoch-1')
            assert json.loads((await work.operation(Request(OWNER, body))).text) == first
            async with factory() as session:
                assert await session.scalar(select(func.count(CashMovement.id))) == 1
                assert await session.scalar(select(func.count(OperationRequest.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
