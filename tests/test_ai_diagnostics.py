"""Real response contracts; fake transport contains no live keys or paid calls."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.miniapp_api import MiniApp
from app.miniapp_security import AccessError
from app.services import ai_diagnostics as diag
from app.services import development_ai as ai


@pytest.mark.parametrize(('status', 'body', 'code', 'retry'), [
    (401, {'error': {'message': 'SECRET'}}, 'invalid_api_key', False),
    (429, {'error': {'code': 'insufficient_quota', 'message': 'SECRET'}}, 'insufficient_quota', False),
    (429, {}, 'rate_limit_exceeded', True),
    (403, {'error': {'code': 'unsupported_country_region_territory'}}, 'unsupported_country_region_territory', False),
    (404, {}, 'model_not_found', False),
    (400, {'error': {'param': 'text.format.schema'}}, 'invalid_schema', False),
    (503, {}, 'provider_unavailable', True),
])
def test_ai_classifies_provider_failure_without_raw_data(status, body, code, retry):
    result = diag.http_failure('test', 'gpt-4.1-mini', status, body)
    assert result['errorCode'] == code and result['retryable'] is retry
    assert 'SECRET' not in json.dumps(diag.public_diagnostics())
    assert 'SECRET' not in json.dumps(result)


def test_compatible_schema_keeps_types_required_and_local_limits():
    original = ai.obj({'value': {'type': 'string', 'minLength': 2, 'maxLength': 5}})
    portable = diag.compatible_schema(original)
    assert portable['required'] == ['value'] and portable['additionalProperties'] is False
    assert portable['properties']['value'] == {'type': 'string'}
    assert not ai.valid_value({'value': 'too long'}, original)


@pytest.mark.parametrize('mode', ['success', 'quota', 'timeout', 'invalid', 'incomplete', 'refusal'])
def test_structured_request_and_safe_failure(monkeypatch, mode):
    calls = []
    class Response:
        status = 429 if mode == 'quota' else 200
        async def __aenter__(self):
            if mode == 'timeout':
                raise TimeoutError()
            return self
        async def __aexit__(self, *args):
            return False
        async def json(self):
            if mode == 'quota':
                return {'error': {'code': 'insufficient_quota', 'message': 'PRIVATE'}}
            data = {'reply': 'Готов', 'score': None, 'errors': [], 'recommendations': []}
            if mode == 'invalid':
                data['reply'] = False
            parts = [{'type': 'refusal'}] if mode == 'refusal' else [{'type': 'output_text', 'text': json.dumps(data)}]
            return {'status': 'incomplete' if mode == 'incomplete' else 'completed', 'output': [{'type': 'message', 'content': parts}]}
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def post(self, url, **kwargs):
            calls.append(kwargs['json'])
            return Response()
    monkeypatch.setattr(ai.aiohttp, 'ClientSession', Client)
    monkeypatch.setattr(ai, 'config', SimpleNamespace(openai_api_key='TEST-ONLY', academy_analysis_model='gpt-4.1-mini'))
    result = asyncio.run(ai.structured('Test', [], ai.ROLEPLAY_SCHEMA, 'test'))
    assert calls[0]['store'] is False
    assert 'maxLength' not in json.dumps(calls[0]['text']['format']['schema'])
    assert result['status'] == ('completed' if mode == 'success' else 'unavailable')
    if mode == 'quota':
        assert result['errorCode'] == 'insufficient_quota'
    assert 'PRIVATE' not in json.dumps(diag.public_diagnostics())


def test_diagnostics_deny_staff_without_owner_role():
    api = MiniApp(None, None, [])
    for role in ('PHOTOGRAPHER', 'MANAGER', 'ADMIN'):
        with pytest.raises(AccessError):
            asyncio.run(api.ai_diagnostics({'miniapp_actor': {'roles': [role]}}))
