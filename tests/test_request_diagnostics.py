"""Production latency diagnostics preserve acknowledgements and redact input."""
import asyncio
import logging
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from app.request_diagnostics import request_diagnostics


def test_operational_acknowledgement_logs_timing_without_credentials_or_guest_data(caplog):
    async def scenario():
        async def handler(request):
            assert (await request.json())['guest'] == 'Private guest'
            return web.json_response({'ok': True, 'bookingId': 17}, status=201)

        app = web.Application(middlewares=[request_diagnostics])
        app.router.add_post('/api/miniapp/bookings/{booking_id}', handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/api/miniapp/bookings/17?token=private-query',
                                         headers={'Authorization': 'Bearer private-header'},
                                         json={'guest': 'Private guest', 'phone': 'private-phone'})
            assert response.status == 201
            assert await response.json() == {'ok': True, 'bookingId': 17}
            request_id = response.headers['X-PhotoBoss-Request-Id']
            assert len(request_id) == 32
            assert request_id in caplog.text

    with caplog.at_level(logging.INFO, logger='app.request_diagnostics'):
        asyncio.run(scenario())
    assert 'route=/api/miniapp/bookings/{booking_id}' in caplog.text
    assert 'status=201 elapsed_ms=' in caplog.text
    assert all(secret not in caplog.text for secret in
               ('Private guest', 'private-query', 'private-header', 'private-phone'))


def test_slow_reads_are_visible_and_fast_reads_are_quiet(caplog):
    async def scenario():
        request = make_mocked_request('GET', '/api/miniapp/bookings?guest=private-query')

        async def handler(request):
            return web.json_response({'ok': True})

        with patch('app.request_diagnostics.monotonic', side_effect=[10, 10.01]):
            await request_diagnostics(request, handler)
        assert not caplog.records
        with patch('app.request_diagnostics.monotonic', side_effect=[10, 12.1]):
            await request_diagnostics(request, handler)

    with caplog.at_level(logging.INFO, logger='app.request_diagnostics'):
        asyncio.run(scenario())
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert 'elapsed_ms=2100' in caplog.text
    assert 'private-query' not in caplog.text


def test_disconnected_write_keeps_cancellation_and_logs_without_payload(caplog):
    async def scenario():
        request = make_mocked_request('POST', '/api/miniapp/attendance/photo')

        async def handler(request):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await request_diagnostics(request, handler)

    with caplog.at_level(logging.INFO, logger='app.request_diagnostics'):
        asyncio.run(scenario())
    assert 'status=499' in caplog.text


def test_non_api_paths_are_not_logged(caplog):
    async def scenario():
        request = make_mocked_request('POST', '/g/private-gallery-token')

        async def handler(request):
            return web.Response(status=200)

        assert (await request_diagnostics(request, handler)).status == 200

    with caplog.at_level(logging.INFO, logger='app.request_diagnostics'):
        asyncio.run(scenario())
    assert not caplog.records
