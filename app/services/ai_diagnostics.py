"""Safe AI error classification. Never retain prompts, images, API keys or raw errors."""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

logger = logging.getLogger(__name__)
_last: dict[str, dict] = {}

MESSAGES = {
    'not_configured': 'OpenAI API не подключён на сервере. Владельцу нужно проверить настройку ключа.',
    'invalid_api_key': 'OpenAI отклонил API-ключ. Владельцу нужно проверить ключ проекта на сервере.',
    'insufficient_quota': 'OpenAI сообщает об исчерпании квоты или лимита расходов API. Владельцу нужно проверить оплату и лимиты API-проекта.',
    'rate_limit_exceeded': 'OpenAI временно ограничил частоту запросов. Повторите после небольшой паузы.',
    'model_not_found': 'Настроенная модель OpenAI недоступна этому API-проекту. Владельцу нужно проверить модель и доступ.',
    'permission_denied': 'API-проекту запрещён этот запрос. Владельцу нужно проверить права ключа и доступ к модели.',
    'unsupported_country_region_territory': 'OpenAI не обслуживает этот регион подключения. Нужен законно доступный провайдер; повтор не исправит ограничение.',
    'invalid_schema': 'OpenAI отклонил формат задания. Требуется исправление серверной схемы запроса.',
    'invalid_request': 'OpenAI отклонил настройки запроса. Подробности доступны в диагностике владельца.',
    'timeout': 'OpenAI не ответил вовремя. Ваш текст сохранён; повторите запрос.',
    'network_error': 'Сервер не смог связаться с OpenAI. Ваш текст сохранён; повторите запрос.',
    'provider_unavailable': 'OpenAI временно недоступен. Ваш текст сохранён; повторите запрос.',
    'incomplete': 'OpenAI вернул незавершённый ответ. Ваш текст сохранён; повторите запрос.',
    'refusal': 'ИИ не смог выполнить это задание. Уточните ответ или выберите другой учебный пример.',
    'invalid_output': 'ИИ вернул ответ, который не прошёл проверку формата. Ваш текст сохранён; повторите запрос.',
}
NONRETRYABLE = {'not_configured', 'invalid_api_key', 'insufficient_quota', 'model_not_found',
                'permission_denied', 'unsupported_country_region_territory', 'invalid_schema', 'invalid_request', 'refusal'}


def safe_label(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', value) else 'unknown'


def record(purpose, model, status, *, code=None, http_status=None):
    entry = {'purpose': safe_label(purpose), 'provider': 'openai', 'model': safe_label(model),
             'status': status, 'checkedAt': datetime.now(UTC).isoformat()}
    if code:
        entry['errorCode'] = safe_label(code)
    if http_status is not None:
        entry['httpStatus'] = http_status
    _last[entry['purpose']] = entry
    if status != 'completed':
        logger.warning('AI result purpose=%s model=%s status=%s http=%s code=%s',
                       entry['purpose'], entry['model'], status, http_status, code)
    else:
        logger.info('AI result purpose=%s model=%s status=completed', entry['purpose'], entry['model'])


def unavailable(purpose, model, code, *, http_status=None):
    if code not in MESSAGES:
        code = 'provider_unavailable'
    record(purpose, model, 'not_configured' if code == 'not_configured' else 'unavailable',
           code=code, http_status=http_status)
    return {'status': 'not_configured' if code == 'not_configured' else 'unavailable',
            'errorCode': code, 'message': MESSAGES[code], 'retryable': code not in NONRETRYABLE}


def http_failure(purpose, model, http_status, body):
    error = body.get('error', {}) if isinstance(body, dict) else {}
    error = error if isinstance(error, dict) else {}
    code = error.get('code') or error.get('type')
    if code in ('billing_hard_limit_reached', 'credit_balance_exhausted'):
        code = 'insufficient_quota'
    if code not in MESSAGES:
        if http_status == 401:
            code = 'invalid_api_key'
        elif http_status == 403:
            code = 'permission_denied'
        elif http_status == 404:
            code = 'model_not_found'
        elif http_status == 429:
            code = 'rate_limit_exceeded'
        elif http_status == 400:
            param = str(error.get('param') or '')
            code = 'invalid_schema' if 'schema' in param or 'format' in param else 'invalid_request'
        else:
            code = 'provider_unavailable'
    return unavailable(purpose, model, code, http_status=http_status)


def public_diagnostics():
    return [dict(value) for value in _last.values()]


def compatible_schema(value):
    """The portable strict subset; local validation still enforces all bounds.

    Avoid legacy-model/provider rejection of string/number bounds. We do not
    relax required keys, object closure, types or enum constraints.
    """
    if isinstance(value, list):
        return [compatible_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    bounds = {'minLength', 'maxLength', 'minimum', 'maximum', 'minItems', 'maxItems'}
    return {key: compatible_schema(item) for key, item in value.items() if key not in bounds}
