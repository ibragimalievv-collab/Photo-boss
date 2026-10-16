"""Regression coverage for real media failure modes; no provider calls."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientPayloadError

from app.miniapp_security import AccessError
from app.workday import Workday


@pytest.mark.parametrize('status', [400, 401, 403, 409, 413, 415, 424, 429, 503])
def test_media_keeps_domain_error_status(status):
    async def run():
        work = Workday(None)
        original = AccessError('Specific safe error', status)
        work.read_media = AsyncMock(side_effect=original)
        with pytest.raises(AccessError) as caught:
            await work.media(SimpleNamespace())
        assert caught.value is original
        assert caught.value.status == status
    asyncio.run(run())


@pytest.mark.parametrize('error', [ConnectionResetError('lost'), ClientPayloadError('truncated'), TimeoutError()])
def test_interrupted_media_is_retryable_not_invalid(error):
    async def run():
        work = Workday(None)
        work.read_media = AsyncMock(side_effect=error)
        with pytest.raises(AccessError) as caught:
            await work.media(SimpleNamespace())
        assert caught.value.status == 503
    asyncio.run(run())


class Part:
    def __init__(self, name, chunks):
        self.name, self.chunks = name, list(chunks)

    async def read_chunk(self, _size):
        return self.chunks.pop(0) if self.chunks else b''

    def at_eof(self):
        return not self.chunks


class Multipart:
    def __init__(self, parts):
        self.parts = list(parts)

    async def next(self):
        return self.parts.pop(0) if self.parts else None


class Request(dict):
    content_type = 'multipart/form-data'

    def __init__(self, parts):
        super().__init__(miniapp_actor={'id': 1})
        self.reader = Multipart(parts)

    async def multipart(self):
        return self.reader


def test_fragmented_metadata_is_accumulated_before_validation():
    async def run():
        body = {'key': 'a-long-operation-key', 'kind': 'shoot_photo'}
        encoded = json.dumps(body).encode()
        request = Request([Part('operation', [encoded[:6], encoded[6:19], encoded[19:]]),
                           Part('file', [b'original ', b'bytes'])])
        work = Workday(None)
        work.execute = AsyncMock(return_value='saved')
        assert await work.media(request) == 'saved'
        work.execute.assert_awaited_once_with({'id': 1}, body, b'original bytes')
    asyncio.run(run())


def test_metadata_limit_applies_to_cumulative_bytes():
    async def run():
        work = Workday(None)
        work.execute = AsyncMock()
        request = Request([Part('operation', [b' ' * 5000, b' ' * 4000]), Part('file', [b'bytes'])])
        with pytest.raises(AccessError) as caught:
            await work.media(request)
        assert caught.value.status == 413
        work.execute.assert_not_awaited()
    asyncio.run(run())
