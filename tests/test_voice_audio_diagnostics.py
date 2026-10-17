import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from app import voice_training as voice
from app.miniapp_security import AccessError
from app.services import ai_diagnostics as diagnostics


class Response:
    def __init__(self, status=200, body=None, *, audio=b'mp3 audio', error=None):
        self.status, self.body, self.audio, self.error = status, body, audio, error
        self.content = self

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    async def iter_chunked(self, size):
        yield self.audio


def provider(monkeypatch, response, *, key='TEST-KEY-NEVER-LOG'):
    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return response

    monkeypatch.setattr(voice, 'config', SimpleNamespace(openai_api_key=key))
    monkeypatch.setattr(voice.aiohttp, 'ClientSession', Session)
    monkeypatch.setattr(diagnostics, '_last', {})


def invoke(mode):
    return voice.transcribe(b'recorded voice', 'audio/webm') if mode == 'transcription' else voice.synthesize('Учебный ответ')


@pytest.mark.parametrize('mode', ['transcription', 'speech'])
@pytest.mark.parametrize('status,code,expected,fragment', [
    (429, 'insufficient_quota', 'insufficient_quota', 'квоты'),
    (401, None, 'invalid_api_key', 'API-ключ'),
    (403, None, 'permission_denied', 'права ключа'),
    (404, None, 'model_not_found', 'модель'),
    (429, None, 'rate_limit_exceeded', 'частоту запросов'),
    (502, 'PRIVATE-UNTRUSTED-CODE', 'provider_unavailable', 'временно недоступен'),
])
def test_provider_audio_errors_are_classified_without_leaking_private_body(monkeypatch, caplog, mode, status, code, expected, fragment):
    provider(monkeypatch, Response(status, {'error': {'code': code, 'message': 'PRIVATE-PROVIDER-TEXT'}}))
    with pytest.raises(AccessError) as error:
        asyncio.run(invoke(mode))
    assert error.value.status == 503
    assert fragment in str(error.value)
    if mode == 'transcription':
        assert 'пока открыта эта страница' in str(error.value)
        assert 'сохранена на устройстве' not in str(error.value)
    entry = diagnostics.public_diagnostics()[0]
    assert entry['purpose'] == 'voice_' + mode
    assert entry['errorCode'] == expected and entry['httpStatus'] == status
    combined = str(error.value) + json.dumps(entry) + caplog.text
    assert all(secret not in combined for secret in ['PRIVATE-PROVIDER-TEXT', 'TEST-KEY-NEVER-LOG', 'PRIVATE-UNTRUSTED-CODE'])


@pytest.mark.parametrize('mode', ['transcription', 'speech'])
@pytest.mark.parametrize('error,code', [(TimeoutError(), 'timeout'), (aiohttp.ClientConnectionError(), 'network_error')])
def test_audio_connection_failures_are_diagnosable(monkeypatch, mode, error, code):
    provider(monkeypatch, Response(error=error))
    with pytest.raises(AccessError) as failure:
        asyncio.run(invoke(mode))
    assert failure.value.status == 503
    assert diagnostics.public_diagnostics()[0]['errorCode'] == code


@pytest.mark.parametrize('text,status', [('', 422), ('x' * 2001, 400)])
def test_transcription_validation_preserves_actionable_status(monkeypatch, text, status):
    provider(monkeypatch, Response(body={'text': text}))
    with pytest.raises(AccessError) as failure:
        asyncio.run(invoke('transcription'))
    assert failure.value.status == status
    assert diagnostics.public_diagnostics()[0]['errorCode'] == 'invalid_output'


def test_audio_success_and_non_json_provider_failure(monkeypatch):
    provider(monkeypatch, Response(body={'text': '  Здравствуйте!  '}))
    assert asyncio.run(invoke('transcription')) == 'Здравствуйте!'
    assert asyncio.run(invoke('speech')) == b'mp3 audio'
    assert all(entry['status'] == 'completed' for entry in diagnostics.public_diagnostics())
    provider(monkeypatch, Response(502, ValueError('PRIVATE-HTML-ERROR')))
    with pytest.raises(AccessError) as failure:
        asyncio.run(invoke('transcription'))
    assert 'PRIVATE-HTML-ERROR' not in str(failure.value)
    assert diagnostics.public_diagnostics()[0]['errorCode'] == 'provider_unavailable'


@pytest.mark.parametrize('mode', ['voice', 'speech'])
def test_saved_voice_actions_cannot_run_during_live_conversation(monkeypatch, mode):
    actor = {'id': 17}
    live = SimpleNamespace(assert_idle=AsyncMock(side_effect=AccessError('Сначала завершите живой разговор.', 409)))
    api = SimpleNamespace(body=AsyncMock(return_value={}))
    service = voice.VoiceTraining(SimpleNamespace(api=api, realtime=live))
    service.session = AsyncMock(return_value={'transcript': '[]', 'status': 'ACTIVE', 'revision': 1})
    provider_call = AsyncMock()
    monkeypatch.setattr(voice, 'transcribe', provider_call)
    monkeypatch.setattr(voice, 'synthesize', provider_call)

    class Request(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.match_info = {'id': '24'}

    with pytest.raises(AccessError) as failure:
        asyncio.run(getattr(service, mode)(Request(miniapp_actor=actor)))
    assert failure.value.status == 409
    live.assert_idle.assert_awaited_once_with(actor, 24)
    provider_call.assert_not_awaited()
    assert service.pending == set()
