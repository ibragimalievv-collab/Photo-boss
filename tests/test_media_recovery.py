"""Regression tests for production upload failures; no private files or providers."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import ClientPayloadError

from app.miniapp_security import AccessError
from app.workday import Workday


class Part:
    def __init__(self, name, chunks):
        self.name, self.chunks = name, list(chunks)

    def at_eof(self):
        return not self.chunks

    async def read_chunk(self, _size):
        value = self.chunks.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class Request(dict):
    def __init__(self, parts):
        super().__init__(miniapp_actor={"id": 7})
        self.parts = iter(parts)

    async def multipart(self):
        return self

    async def next(self):
        return next(self.parts, None)


def test_operation_metadata_can_arrive_in_separate_network_packets():
    body = {"key": "test-upload-key-123", "kind": "shoot_photo", "date": "2026-10-16",
            "actorId": 7, "data": {"shooting": {"id": 1}}}
    raw = json.dumps(body).encode()
    request = Request([Part("operation", [raw[:8], raw[8:31], raw[31:]]), Part("file", [b"abc", b"def"])])
    service = Workday(SimpleNamespace())

    async def execute(actor, value, content):
        assert actor["id"] == 7 and value == body and content == b"abcdef"
        return "accepted"

    service.execute = execute
    assert asyncio.run(service.media(request)) == "accepted"


@pytest.mark.parametrize("status", [400, 401, 403, 409, 413, 424, 429, 503])
def test_domain_http_status_survives_media_boundary(status):
    service = Workday(None)
    expected = AccessError("Storage or permission result", status)

    async def read(_request):
        raise expected

    service.read_media = read
    with pytest.raises(AccessError) as captured:
        asyncio.run(service.media(None))
    assert captured.value is expected
    assert captured.value.status == status


@pytest.mark.parametrize("failure", [ConnectionResetError("client left"), ClientPayloadError("partial file"), TimeoutError()])
def test_interrupted_media_is_retryable(failure):
    service = Workday(None)

    async def read(_request):
        raise failure

    service.read_media = read
    with pytest.raises(AccessError) as captured:
        asyncio.run(service.media(None))
    assert captured.value.status == 503


def test_metadata_limit_still_enforced():
    request = Request([Part("operation", [b"x" * 5000, b"y" * 4000])])
    with pytest.raises(AccessError) as captured:
        asyncio.run(Workday(None).media(request))
    assert captured.value.status == 413


def test_missing_file_is_still_bad_request():
    request = Request([Part("operation", [b"{}"])])
    with pytest.raises(AccessError) as captured:
        asyncio.run(Workday(None).media(request))
    assert captured.value.status == 400
