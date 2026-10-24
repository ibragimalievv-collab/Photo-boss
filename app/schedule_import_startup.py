"""Opt-in owner-authorized deployment request; never a public bypass endpoint."""
from __future__ import annotations

import json
import logging
import re

from .audit_context import actor_id
from .miniapp_security import AccessError, role_permissions
from .schedule_import import RECEIPT_PREFIX, positive

logger = logging.getLogger(__name__)
REQUEST_ENV = 'PHOTO_BOSS_SCHEDULE_TEXT_REQUEST'
PRODUCTION_SERVICE = 'srv-damohb1mirbc73as0rl0'


def normalized(value):
    return str(value).casefold().replace('ё', 'е').strip()


async def startup_schedule_import(service, raw, admin_ids, *, service_id):
    """Missing/ambiguous identity or settings are reported, never guessed.

    A retained request is replay-safe even after a later manual schedule edit:
    an existing receipt is only verified, not applied again with a new revision.
    """
    if not raw.strip():
        return None
    if service_id != PRODUCTION_SERVICE:
        logger.warning('Schedule text request blocked: unexpected service')
        return {'status': 'blocked', 'reason': 'unexpected_service'}
    try:
        request = json.loads(raw)
        if (not isinstance(request, dict) or set(request) - {'requestKey', 'employeeName', 'userId', 'text', 'defaults'}
                or not {'requestKey', 'text'}.issubset(request)
                or ('employeeName' in request) == ('userId' in request)
                or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', str(request.get('requestKey', '')))):
            raise AccessError('Некорректный запрос импорта графика.', 400)
        api = service.api
        async with api.engine.begin() as conn:
            owners = await api.rows(conn, """SELECT DISTINCT u.id,u.tg_id,u.name FROM users u
                JOIN user_roles r ON r.user_id=u.id AND r.role='OWNER'
                WHERE u.active=TRUE ORDER BY u.id""")
            owner = next((dict(row) for row in owners if row['tg_id'] in admin_ids), None)
            if owner is None:
                raise AccessError('Нет подтверждённого активного владельца для импорта.', 403)
            actor = {**owner, 'roles': ['OWNER'], 'permissions': role_permissions(['OWNER'])}
            if 'userId' in request:
                uid = positive(request['userId'])
            else:
                name = normalized(request['employeeName'])
                if not name or len(name) > 150:
                    raise AccessError('Нужно имя сотрудника.', 400)
                roster = await api.rows(conn, """SELECT DISTINCT u.id,u.name FROM users u JOIN user_roles r ON r.user_id=u.id
                    WHERE u.active=TRUE AND r.role IN ('MANAGER','PHOTOGRAPHER') ORDER BY u.id""")
                found = [dict(r) for r in roster if normalized(r['name']) == name or name in normalized(r['name']).split()]
                if len(found) != 1:
                    candidates = found or [dict(r) for r in roster if name in normalized(r['name'])
                        or (name == 'лиза' and 'елизавета' in normalized(r['name']).split())]
                    result = {'status': 'needs_confirmation', 'reason': 'employee', 'candidates': candidates}
                    logger.warning('Schedule text request: %s', json.dumps(result, ensure_ascii=False))
                    return result
                uid = found[0]['id']
            body = {'userId': uid, 'text': request['text'], 'defaults': request.get('defaults', {})}
            uid, parsed, defaults = service.payload(body)
            grants = await service.authorize(conn, actor, uid)
            key = f"{RECEIPT_PREFIX}{actor['id']}:{request['requestKey']}"
            prior = await api.rows(conn, 'SELECT value FROM settings WHERE key=:key', key=key)
            if not prior:
                preview, *_ = await service.prepare(conn, actor, uid, parsed, defaults, grants)
                if not preview['canApply']:
                    result = {'status': 'needs_confirmation', 'reason': 'settings', 'employee': preview['employee'],
                              'needs': preview['needs'], 'counts': preview['counts']}
                    logger.warning('Schedule text request: %s', json.dumps(result, ensure_ascii=False))
                    return result
        token = actor_id.set(actor['id'])
        try:
            if prior:
                # apply_body checks both identity/payload fingerprint and current saved result.
                result = await service.apply_body(actor, {**body, 'requestKey': request['requestKey'], 'revision': '0'*64})
            else:
                result = await service.apply_body(actor, {**body, 'requestKey': request['requestKey'], 'revision': preview['revision']})
        finally:
            actor_id.reset(token)
        logger.info('Schedule text request VERIFIED: %s', json.dumps(result, ensure_ascii=False))
        return {'status': 'verified', **result}
    except (AccessError, ValueError, TypeError) as exc:
        logger.warning('Schedule text request blocked without partial application: %s', str(exc))
        return {'status': 'blocked', 'reason': str(exc)}
