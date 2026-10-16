import asyncio
from types import SimpleNamespace

import pytest

from app.services import ai_diagnostics as diagnostic
from app.services import development_ai as ai


@pytest.mark.parametrize(('status', 'body', 'code'), [
    (401, {}, 'invalid_api_key'),
    (429, {'error': {'code': 'insufficient_quota', 'message': 'SECRET'}}, 'insufficient_quota'),
    (429, {}, 'rate_limit_exceeded'),
    (404, {}, 'model_not_found'),
    (400, {'error': {'param': 'text.format.schema'}}, 'invalid_schema'),
    (403, {}, 'permission_denied'),
    (503, {}, 'provider_unavailable'),
])
def test_safe_error_classification(status, body, code):
    result = diagnostic.http_failure('sales_training', 'gpt-4.1-mini', status, body)
    assert result['errorCode'] == code
    assert 'SECRET' not in str(result)
    assert 'SECRET' not in str(diagnostic.public_diagnostics())
    if code == 'insufficient_quota':
        assert not result['retryable']
        assert 'API' in result['message']


def test_portable_schema_keeps_structure_and_local_bounds():
    original = ai.obj({'text': {'type': 'string', 'minLength': 1, 'maxLength': 10},
                       'n': {'type': 'integer', 'minimum': 0, 'maximum': 2}})
    portable = diagnostic.compatible_schema(original)
    assert portable['required'] == ['text', 'n']
    assert portable['additionalProperties'] is False
    assert portable['properties']['text'] == {'type': 'string'}
    assert not ai.valid_value({'text': 'long text longer than ten', 'n': 9}, original)
    assert original['properties']['n']['maximum'] == 2


def test_structured_error_and_success_without_real_api(monkeypatch):
    current = {'status': 429, 'body': {'error': {'code': 'insufficient_quota'}}}
    class Response:
        @property
        def status(self): return current['status']
        async def json(self): return current['body']
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def post(self, url, **kwargs):
            assert kwargs['json']['store'] is False
            assert kwargs['json']['text']['format']['schema']['properties']['reply'] == {'type': 'string'}
            return Response()
    monkeypatch.setattr(ai, 'config', SimpleNamespace(openai_api_key='PRIVATE_TEST_KEY', academy_analysis_model='gpt-4.1-mini'))
    monkeypatch.setattr(ai.aiohttp, 'ClientSession', Client)
    result = asyncio.run(ai.structured('test', [], ai.ROLEPLAY_SCHEMA, 'test_probe'))
    assert result['errorCode'] == 'insufficient_quota'
    assert 'PRIVATE_TEST_KEY' not in str(diagnostic.public_diagnostics())
    current.update(status=200, body={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"reply":"Привет","score":null,"errors":[],"recommendations":[]}'}]}]})
    result = asyncio.run(ai.structured('test', [], ai.ROLEPLAY_SCHEMA, 'test_probe'))
    assert result['status'] == 'completed' and result['data']['reply'] == 'Привет'


def test_structured_timeout_is_actionable(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def post(self, *args, **kwargs): raise TimeoutError()
    monkeypatch.setattr(ai, 'config', SimpleNamespace(openai_api_key='TEST', academy_analysis_model='gpt-4.1-mini'))
    monkeypatch.setattr(ai.aiohttp, 'ClientSession', Client)
    result = asyncio.run(ai.structured('test', [], ai.ROLEPLAY_SCHEMA, 'test_timeout'))
    assert result['errorCode'] == 'timeout' and result['retryable']
    assert 'сохранён' in result['message']
