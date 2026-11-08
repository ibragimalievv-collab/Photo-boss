"""Reusable, individually revocable staff QR credentials.

Only the random grant identifier is stored. The bearer is reproduced using a
purpose-separated server HMAC and is transported in a URL fragment / POST body.
It never contains the employee's password or grants a client-supplied role.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
from io import BytesIO
import json
import logging
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web
import qrcode
from sqlalchemy import text

from . import miniapp_sessions as sessions
from .browser_login import BrowserLogin, COOKIE as APPROVAL_COOKIE, PREFIX as APPROVAL_PREFIX
from .miniapp_security import AccessError, STAFF_ROLES

logger = logging.getLogger(__name__)
USER_PREFIX = 'miniapp:qr:user:'
GRANT_PREFIX = 'miniapp:qr:grant:'
RATE_PREFIX = 'miniapp:qr:rate:'
GRANT_RE = re.compile(r'^[A-Za-z0-9_-]{32}$')
TOKEN_RE = re.compile(r'^v1\.([A-Za-z0-9_-]{32})\.([A-Za-z0-9_-]{43})$')
RATE_TTL = 300
RATE_MAX = 120
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff'}
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def parse_grant(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if (not isinstance(value, dict) or set(value) != {'id', 'roles', 'createdAt'}
                or not isinstance(value['id'], str) or not GRANT_RE.fullmatch(value['id'])
                or not isinstance(value['roles'], list) or not value['roles']
                or any(not isinstance(r, str) or r not in STAFF_ROLES for r in value['roles'])
                or value['roles'] != sorted(set(value['roles']))
                or not isinstance(value['createdAt'], str) or len(value['createdAt']) > 64):
            return None
        return value
    except (ValueError, TypeError, KeyError):
        return None


async def read_grant(conn, uid):
    raw = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                             {'key': USER_PREFIX + str(uid)})).scalar()
    return parse_grant(raw)


async def session_grant_roles(conn, uid, grant_id):
    """Return the checked role snapshot for this authorization attempt."""
    if type(uid) is not int or not 0 < uid < 2**31 or not isinstance(grant_id, str):
        return None
    if not GRANT_RE.fullmatch(grant_id):
        return None
    grant = await read_grant(conn, uid)
    if not grant or grant['id'] != grant_id:
        return None
    index = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                               {'key': GRANT_PREFIX + grant_id})).scalar()
    if index != str(uid):
        return None
    active = (await conn.execute(text('SELECT active FROM users WHERE id=:uid'),
                                {'uid': uid})).scalar()
    if not active:
        return None
    roles = (await conn.execute(text('SELECT role FROM user_roles WHERE user_id=:uid'),
                               {'uid': uid})).scalars().all()
    return list(grant['roles']) if sorted(set(roles)) == grant['roles'] else None


async def valid_session_grant(conn, uid, grant_id):
    """Validate QR provenance without treating a remembered session as a bearer."""
    return (await session_grant_roles(conn, uid, grant_id)) is not None


def qr_manageable(roles, actor_id, target, *, owner_access_granted=False):
    roles, target_roles = set(roles), set(target['roles'])
    if (not target.get('active') or not target_roles or not target_roles <= STAFF_ROLES):
        return False
    if 'OWNER' in roles:
        # A delegated owner must not impersonate the primary owner to bypass
        # the separate owner-access administration boundary.
        return (target['id'] == actor_id or 'OWNER' not in target_roles
                or not owner_access_granted)
    return bool('ADMIN' in roles and target['id'] != actor_id
                and not target_roles & {'OWNER', 'ADMIN'})


async def revoke_qr_sessions(conn, uid):
    """Remove QR devices only; preserve independent password/Telegram devices."""
    rows = (await conn.execute(text('SELECT key,value FROM settings WHERE key LIKE :prefix'),
                               {'prefix': f'miniapp:device:{int(uid)}:%'})).all()
    for key, raw in rows:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and value.get('auth_method') == 'qr':
            await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': key})


async def revoke_for_user(conn, uid):
    """Caller holds the user's ordinary row lock, shared with device issuance."""
    grant = await read_grant(conn, uid)
    await conn.execute(text('DELETE FROM settings WHERE key=:key'),
                       {'key': USER_PREFIX + str(uid)})
    if grant:
        await conn.execute(text('DELETE FROM settings WHERE key=:key'),
                           {'key': GRANT_PREFIX + grant['id']})
    await revoke_qr_sessions(conn, uid)
    return bool(grant)


class StaffQRLogin:
    def __init__(self, api, origin):
        from .people import People
        self.api, self.engine = api, api.engine
        self.browser = BrowserLogin(api.engine, api.bot, origin)
        self.origin = self.browser.origin
        self.allowed_origins = {self.origin}
        self.people = People(api)
        if getattr(api, 'standalone', False):
            key = getattr(api, 'qr_signing_key', None)
            if not isinstance(key, bytes) or len(key) != 32:
                raise ValueError('A separate 32-byte QR signing key is required')
            self.signing_key = key
        else:
            self.signing_key = hmac.new(api.bot.token.encode(),
                                        b'photo-boss:staff-qr-login:v1', hashlib.sha256).digest()

    def guard(self, request):
        supplied = request.headers.get('Origin', '')
        if supplied not in self.allowed_origins:
            raise AccessError('Запрос входа отклонён. Откройте сайт Photo Boss.', 403)
        expected = urlsplit(supplied)
        if expected.scheme != 'https' or request.host != expected.netloc:
            raise AccessError('Запрос входа отклонён. Откройте сайт Photo Boss.', 403)
        BrowserLogin(self.engine, self.api.bot, supplied).guard(request)

    def token(self, uid, grant):
        payload = json.dumps([self.origin, int(uid), grant['id'], grant['roles']],
                             separators=(',', ':'), ensure_ascii=True).encode()
        signature = base64.urlsafe_b64encode(hmac.new(self.signing_key, payload,
                                                    hashlib.sha256).digest()).decode().rstrip('=')
        return 'v1.' + grant['id'] + '.' + signature

    def representation(self, target, grant):
        url = self.origin + '/staff-login/#token=' + self.token(target['id'], grant)
        image = qrcode.make(url, box_size=8, border=4)
        output = BytesIO()
        image.save(output, format='PNG')
        return {'enabled': True, 'employee': self.employee(target),
                'image': 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode(),
                'url': url, 'createdAt': grant['createdAt']}

    @staticmethod
    def employee(target):
        return {key: target[key] for key in ('id', 'name', 'roles')}

    async def target_for_editor(self, conn, request):
        from .people import positive_id
        uid = positive_id(request.match_info['id'])
        actor = request['miniapp_actor']
        roles = await self.people.current_editor(conn, actor['id'], uid)
        editor = await self.people.card(conn, actor['id'])
        target = await self.people.card(conn, uid)
        if not qr_manageable(roles, actor['id'], target,
                             owner_access_granted=editor['ownerAccessGranted']):
            raise AccessError('QR-вход для этого аккаунта вам недоступен.', 403)
        return target

    async def empty_body(self, request, *, optional=False):
        if optional and not request.content_length and request.content.at_eof():
            return
        if await self.api.body(request) != {}:
            raise AccessError('Лишние параметры QR-входа.', 400)

    async def status(self, request):
        async with self.engine.begin() as conn:
            target = await self.target_for_editor(conn, request)
            grant = await read_grant(conn, target['id'])
            valid = bool(grant and await valid_session_grant(conn, target['id'], grant['id']))
        return web.json_response({'enabled': valid, 'employee': self.employee(target),
                                  'createdAt': grant['createdAt'] if valid else None}, headers=HEADERS)

    async def create_grant(self, conn, uid, roles):
        await revoke_for_user(conn, uid)
        grant = {'id': secrets.token_urlsafe(24), 'roles': sorted(set(roles)),
                 'createdAt': datetime.now(timezone.utc).isoformat()}
        await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) '
                                'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                           {'key': USER_PREFIX + str(uid), 'value': json.dumps(grant)})
        await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value)'),
                           {'key': GRANT_PREFIX + grant['id'], 'value': str(uid)})
        return grant

    async def show(self, request):
        self.guard(request)
        await self.empty_body(request)
        async with self.engine.begin() as conn:
            target = await self.target_for_editor(conn, request)
            grant = await read_grant(conn, target['id'])
            if not grant or not await valid_session_grant(conn, target['id'], grant['id']):
                grant = await self.create_grant(conn, target['id'], target['roles'])
                await self.api.audit_write(conn, request['miniapp_actor'], 'staff_qr_created',
                                           'user', target['id'], 'Создан личный QR-вход сотрудника.')
            result = self.representation(target, grant)
        return web.json_response(result, headers=HEADERS)

    async def rotate(self, request):
        self.guard(request)
        await self.empty_body(request)
        async with self.engine.begin() as conn:
            target = await self.target_for_editor(conn, request)
            grant = await self.create_grant(conn, target['id'], target['roles'])
            await self.api.audit_write(conn, request['miniapp_actor'], 'staff_qr_rotated',
                                       'user', target['id'], 'Личный QR-вход заменён; прежний QR отключён.')
            result = self.representation(target, grant)
        return web.json_response(result, headers=HEADERS)

    async def revoke(self, request):
        self.guard(request)
        await self.empty_body(request, optional=True)
        async with self.engine.begin() as conn:
            target = await self.target_for_editor(conn, request)
            changed = await revoke_for_user(conn, target['id'])
            if changed:
                await self.api.audit_write(conn, request['miniapp_actor'], 'staff_qr_revoked',
                                           'user', target['id'], 'Личный QR-вход отключён.')
        return web.json_response({'enabled': False, 'employee': self.employee(target)}, headers=HEADERS)

    async def rate_limit(self, request):
        now = int(time.time())
        peer = getattr(request, 'remote', None) or 'unknown'
        fingerprint = hmac.new(self.signing_key, str(peer).encode(), hashlib.sha256).hexdigest()
        key = f'{RATE_PREFIX}{now // RATE_TTL:010d}:{fingerprint}'
        async with self.engine.begin() as conn:
            count = (await conn.execute(text("INSERT INTO settings(key,value) VALUES (:key,'1') "
                'ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(settings.value AS INTEGER)+1 AS TEXT) '
                'RETURNING value'), {'key': key})).scalar_one()
            await conn.execute(text('DELETE FROM settings WHERE key LIKE :prefix AND key < :cutoff'),
                               {'prefix': RATE_PREFIX + '%',
                                'cutoff': f'{RATE_PREFIX}{now // RATE_TTL - 1:010d}:'})
        if int(count) > RATE_MAX:
            raise AccessError('Слишком много попыток. Повторите через пять минут.', 429)

    async def public_token(self, request):
        self.guard(request)
        await self.rate_limit(request)
        body = await self.api.body(request, max_bytes=1024)
        if getattr(request, 'query', {}) or set(body) != {'token'}:
            raise AccessError('Нужен только личный QR-код без дополнительных параметров.', 400)
        if not isinstance(body['token'], str) or not TOKEN_RE.fullmatch(body['token']):
            raise AccessError('QR-код недействителен или отключён. Попросите новый QR.', 401)
        return body['token']

    async def token_user(self, conn, token):
        match = TOKEN_RE.fullmatch(token)
        if not match:
            raise AccessError('QR-код недействителен или отключён. Попросите новый QR.', 401)
        raw = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                                 {'key': GRANT_PREFIX + match[1]})).scalar()
        if not isinstance(raw, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', raw) or int(raw) >= 2**31:
            raise AccessError('QR-код недействителен или отключён. Попросите новый QR.', 401)
        return int(raw)

    async def token_target(self, conn, token, uid):
        grant = await read_grant(conn, uid)
        if (not grant or not await valid_session_grant(conn, uid, grant['id'])
                or not hmac.compare_digest(self.token(uid, grant), token)):
            raise AccessError('QR-код недействителен или отключён. Попросите новый QR.', 401)
        target = await self.people.card(conn, uid)
        if not target['active'] or not STAFF_ROLES.intersection(target['roles']):
            raise AccessError('QR-код недействителен или отключён. Попросите новый QR.', 401)
        return target, grant

    async def lock_users(self, conn, ids):
        from .db import user_lock_clause
        for uid in sorted(set(ids)):
            await conn.execute(text('SELECT id FROM users WHERE id=:uid' + user_lock_clause(conn)),
                               {'uid': uid})

    async def inspect(self, request):
        token = await self.public_token(request)
        async with self.engine.connect() as conn:
            uid = await self.token_user(conn, token)
            target, _ = await self.token_target(conn, token, uid)
        return web.json_response({'employee': self.employee(target)}, headers=HEADERS)

    async def login(self, request):
        token = await self.public_token(request)
        async with self.engine.begin() as conn:
            uid = await self.token_user(conn, token)
            old = sessions.cookie(request)
            old_uid = int(old.split('.')[0]) if sessions.session_key(old) else uid
            await self.lock_users(conn, [uid, old_uid] if old_uid < 2**31 else [uid])
            target, grant = await self.token_target(conn, token, uid)
            # Only the current device is replaced. Other ordinary logins are
            # independent; no Telegram freshness is inherited by a QR login.
            if sessions.session_key(old):
                await conn.execute(text('DELETE FROM settings WHERE key=:key'),
                                   {'key': sessions.session_key(old)})
            pending = getattr(request, 'cookies', {}).get(APPROVAL_COOKIE, '').split('.')[0]
            if re.fullmatch(r'[A-Za-z0-9_-]{24}', pending):
                await conn.execute(text('DELETE FROM settings WHERE key=:key'),
                                   {'key': APPROVAL_PREFIX + pending})
            raw = await sessions.issue(conn, {'id': uid, 'tg_id': target['telegramId']},
                                       '', int(time.time()), qr_grant_id=grant['id'])
            if not raw:
                raise AccessError('QR-вход недоступен. Попросите новый QR.', 401)
            await self.api.audit_write(conn, {'id': uid}, 'staff_qr_login', 'user', uid,
                                       'Выполнен вход по личному QR-коду.')
        response = web.json_response({'status': 'authenticated'}, headers=HEADERS)
        sessions.set_cookie(response, raw)
        response.del_cookie(APPROVAL_COOKIE, path='/', secure=True, httponly=True, samesite='Strict')
        return response

    async def endpoint(self, request):
        try:
            if request.path == '/auth/qr/inspect':
                return await self.inspect(request)
            if request.path == '/auth/qr/login':
                return await self.login(request)
            raise AccessError('Недопустимый запрос.', 404)
        except AccessError as error:
            return web.json_response({'error': str(error)}, status=error.status, headers=HEADERS)
        except Exception:
            logger.exception('Staff QR authentication unavailable')
            return web.json_response({'error': 'QR-вход временно недоступен. Повторите попытку.'},
                                     status=503, headers=HEADERS)

    async def static(self, request):
        asset = request.match_info.get('asset', 'index.html')
        if asset not in {'index.html', 'login.js', 'login.css', 'scan.js', 'scan.css',
                         'decoder.js', 'decoder-LICENSE.txt'}:
            raise web.HTTPNotFound()
        headers = dict(HEADERS, **{'Content-Security-Policy': PAGE_CSP})
        return web.FileResponse(Path(__file__).parent / 'staff_qr_ui' / asset, headers=headers)


def install_staff_qr_login(app, api):
    from .launch_policy import app_url
    service = StaffQRLogin(api, app_url().split('/app/', 1)[0])
    app['staff_qr_login'] = service
    root = '/api/miniapp/people/{id}/login-qr'
    app.router.add_get(root, service.status)
    app.router.add_post(root, service.show)
    app.router.add_post(root + '/rotate', service.rotate)
    app.router.add_delete(root, service.revoke)
    app.router.add_post('/auth/qr/inspect', service.endpoint)
    app.router.add_post('/auth/qr/login', service.endpoint)
    app.router.add_get('/staff-login/', service.static)
    app.router.add_get(r'/staff-login/{asset:login\.js|login\.css|scan\.js|scan\.css|decoder\.js|decoder-LICENSE\.txt}', service.static)
    return service
