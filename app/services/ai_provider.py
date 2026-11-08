"""Explicit provider selection for Responses requests; credentials stay server-side."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
import uuid


@dataclass(frozen=True)
class ResponsesProfile:
    provider: str
    endpoint: str
    model: str
    headers: dict = field(repr=False)
    configured: bool = False
    production_text: bool = False


def responses_profile(config, *, receipt=False, vision=False):
    provider = getattr(config, 'ai_provider', 'openai')
    if provider == 'openai':
        key = config.openai_api_key
        model = config.receipt_analysis_model if receipt else config.academy_analysis_model
        return ResponsesProfile(provider, 'https://api.openai.com/v1/responses', model,
                                {'Authorization': 'Bearer ' + key}, bool(key))
    if provider != 'yandex':
        raise ValueError('AI_PROVIDER: неизвестный провайдер')
    key = getattr(config, 'yandex_cloud_api_key', '')
    folder = getattr(config, 'yandex_cloud_folder_id', '')
    visual = receipt or vision
    model = (getattr(config, 'yandex_vision_model', 'qwen3.6-35b-a3b') if visual else
             getattr(config, 'yandex_text_model', 'aliceai-llm-flash'))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', folder or ''):
        raise ValueError('YANDEX_CLOUD_FOLDER_ID: некорректный каталог')
    production_text = (not visual and model == 'deepseek-v4.1-flash'
        and getattr(config, 'yandex_text_live', False) is True
        and not os.environ.get('PHOTO_BOSS_AI_COMPARE_BUDGET'))
    if not visual and model in {'aliceai-llm', 'deepseek-v4.1-flash'} and not production_text:
        # This is an isolated, bounded comparison, never a production default.
        # Availability is read-only; the spend decision is rechecked under the
        # shared journal lock by reserve_comparison_response before network I/O.
        try:
            from tools.ai_compare_budget import _load, _path
            budget = os.environ.get('PHOTO_BOSS_AI_COMPARE_BUDGET', '')
            if not budget:
                raise ValueError
            _load(_path(budget), require_existing=True)
        except Exception:
            raise ValueError('Эта модель доступна только в закрытом сравнении с существующим общим лимитом.') from None
    if ((visual and model != 'qwen3.6-35b-a3b')
            or (not visual and model not in {'qwen3.6-35b-a3b', 'aliceai-llm-flash', 'aliceai-llm', 'deepseek-v4.1-flash'})):
        raise ValueError('YANDEX AI: модель ещё не проверена')
    return ResponsesProfile(provider, 'https://ai.api.cloud.yandex.net/v1/responses',
                            'gpt://' + folder + '/' + model,
                            {'Authorization': 'Api-Key ' + key, 'OpenAI-Project': folder,
                             'x-data-logging-enabled': 'false'},
                            bool(key) and (not visual or getattr(config, 'yandex_vision_live', True) is True),
                            production_text)


def ai_configured(config, *, vision=False):
    if getattr(config, 'ai_provider', 'openai') == 'openai':
        return bool(config.openai_api_key)
    return responses_profile(config, vision=vision).configured


def response_text(body):
    """Read both documented Responses envelopes without bypassing refusals."""
    if not isinstance(body, dict):
        raise ValueError('invalid_response_envelope')
    output_items = body.get('output', [])
    if not isinstance(output_items, list):
        raise ValueError('invalid_response_envelope')
    parts = []
    for item in output_items:
        if not isinstance(item, dict):
            raise ValueError('invalid_response_envelope')
        if item.get('type') != 'message':
            continue
        content = item.get('content', [])
        if not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
            raise ValueError('invalid_response_envelope')
        if any(part.get('type') == 'output_text' and not isinstance(part.get('text'), str)
               for part in content):
            raise ValueError('invalid_response_envelope')
        parts.extend(content)
    if any(part.get('type') == 'refusal' for part in parts):
        raise ValueError('provider_refusal')
    output = ''.join(part['text'] for part in parts
                     if part.get('type') == 'output_text' and isinstance(part.get('text'), str))
    output = output or body.get('output_text')
    if not isinstance(output, str) or not output.strip():
        raise ValueError('missing_response_text')
    return output


def structured_json(text):
    """Ambiguous repeated keys cannot silently replace an image or criterion."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_structured_key')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique)


class ComparisonLimitError(RuntimeError):
    """The private comparison is capped; production requests have no test hook."""


def reserve_comparison_response(profile, payload, *, allow_extended_text=False):
    path = os.environ.get('PHOTO_BOSS_AI_COMPARE_BUDGET', '')
    if not path:
        if profile.provider == 'yandex' and profile.model.endswith('/aliceai-llm'):
            # A previously built Full profile cannot outlive its comparison
            # mode and accidentally become an unbudgeted production request.
            raise ComparisonLimitError('full_alice_requires_comparison_budget')
        if (profile.provider == 'yandex' and profile.model.endswith('/deepseek-v4.1-flash')
                and not profile.production_text):
            raise ComparisonLimitError('deepseek_requires_comparison_budget')
        return
    try:
        from tools.ai_compare_budget import reserve_yandex
        # This optional entry is for a bounded, isolated mentor acceptance only.
        # Image benchmarks use their own reservations. No automatic retries.
        known = re.fullmatch(r'gpt://[A-Za-z0-9_-]{1,64}/(aliceai-llm-flash|aliceai-llm|qwen3\.6-35b-a3b|deepseek-v4\.1-flash)',
                             profile.model or '')
        model_name = known[1] if known else None
        kind, byte_limit, token_limit = {
            'aliceai-llm-flash': ('text_alice', 60_000, 5000),
            'aliceai-llm': ('text_alice_full', 120_000, 4500),
            'qwen3.6-35b-a3b': ('text', 240_000, 5000),
            'deepseek-v4.1-flash': ('text_deepseek_v41', 60_000, 4500),
        }.get(model_name, (None, 0, 0))
        if (allow_extended_text is True and model_name == 'deepseek-v4.1-flash'
                and type(payload.get('max_output_tokens')) is int
                and payload['max_output_tokens'] > 4500):
            kind, token_limit = 'text_deepseek_v41_long', 6500
        request_raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if (profile.provider != 'yandex'
                or kind is None
                or payload.get('model') != profile.model
                or type(payload.get('max_output_tokens')) is not int
                or not 0 < payload['max_output_tokens'] <= token_limit
                or len(request_raw) > byte_limit
                or payload.get('tools')
                or not isinstance(payload.get('input'), list) or not payload['input']
                or any(part.get('type') != 'input_text'
                       or not isinstance(part.get('text'), str)
                       for item in payload.get('input', []) for part in item.get('content', []))):
            raise ComparisonLimitError('comparison_request_not_bounded')
        attempt = 'mentor-' + uuid.uuid4().hex
        directory = Path(path).parent / 'provider-responses'
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or directory.is_symlink()
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ComparisonLimitError('private_comparison_output_required')
        reservation = reserve_yandex(path, attempt, kind, require_existing=True)
        fd = os.open(directory / (attempt + '.request.json'),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(request_raw)
            target.flush()
            os.fsync(target.fileno())
        return reservation | {'attemptId': attempt, 'artifact': str(directory / (attempt + '.json')),
                              'requestPayloadBytes': len(request_raw)}
    except ComparisonLimitError:
        raise
    except Exception:
        raise ComparisonLimitError('comparison_budget_unavailable') from None


def capture_comparison_response(reservation, profile, http_status, body):
    """Keep final billing evidence private before application validation."""
    if reservation is None:
        return
    try:
        raw = json.dumps({'attemptId': reservation['attemptId'], 'provider': profile.provider,
            'model': profile.model, 'httpStatus': http_status, 'body': body,
            'requestPayloadBytes': reservation.get('requestPayloadBytes')},
            ensure_ascii=False, allow_nan=False).encode()
        fd = os.open(reservation['artifact'], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        raise ComparisonLimitError('comparison_evidence_unavailable') from None
