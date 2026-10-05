"""Personal passwords bound to server-verified staff, using existing settings storage."""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time

from aiohttp import web
from sqlalchemy import text

from . import miniapp_sessions as sessions
from .browser_login import COOKIE as APPROVAL_COOKIE
from .browser_login import PREFIX as APPROVAL_PREFIX
from .browser_login import BrowserLogin
from .miniapp_security import (
    AccessError,
    validate_init_data,
    validate_owner_launch_token,
)

logger = logging.getLogger(__name__)
CREDENTIAL_PREFIX = 'miniapp:password:'
LOGIN_PREFIX = 'miniapp:password-login:'
RATE_PREFIX = 'miniapp:password-rate:'
VERIFY_TTL = 600
LOGIN_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{2,49}$')


def normalize_login(value):
    if not isinstance(value, str) or not LOGIN_RE.fullmatch(value.strip().lower()):
        raise AccessError('Логин: 3–50 латинских букв, цифр, точек, дефисов или подчёркиваний.', 400)
    return value.strip().lower()


def password_value(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 128 or len(value.encode()) > 512:
        raise AccessError('Пароль должен содержать от 10 до 128 символов.', 400)
    return value


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return f'scrypt-v1${salt}${digest.hex()}'


def password_matches(password, stored):
    try:
        version, salt, digest = stored.split('$')
        if version != 'scrypt-v1' or len(salt) != 32 or len(digest) != 64:
            return False
        return hmac.compare_digest(password_hash(password, salt), stored)
    except (ValueError, TypeError, AttributeError):
        return False


class PasswordLogin:
    def __init__(self, api, origin):
        self.api = api
        self.browser = BrowserLogin(api.engine, api.bot, origin)
        # A missing login still pays the same password hashing cost.
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        self.hash_slots = asyncio.Semaphore(2)

    async def hash_work(self, function, *args):
        async with self.hash_slots:
            return await asyncio.to_thread(function, *args)

    async def rate_limit(self, request, scope):
        now = int(time.time())
        fingerprints = [(request.remote or 'unknown', 20), (scope, 10)]
        limited = False
        async with self.api.engine.begin() as conn:
            for index, (value, maximum) in enumerate(fingerprints):
                fingerprint = hmac.new(self.api.bot.token.encode(), value.encode(), hashlib.sha256).hexdigest()
                key = f'{RATE_PREFIX}{now // 300:010d}:{index}:{fingerprint}'
                count = (await conn.execute(text("INSERT INTO settings(key,value) VALUES (:key,'1') "
                    'ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(settings.value AS INTEGER)+1 AS TEXT) '
                    'RETURNING value'), {'key': key})).scalar_one()
                limited |= int(count) > maximum
            await conn.execute(text('DELETE FROM settings WHERE key LIKE :prefix AND key < :cutoff'),
                {'prefix': RATE_PREFIX + '%', 'cutoff': f'{RATE_PREFIX}{now // 300 - 1:010d}:'})
        if limited:
            raise AccessError('Слишком много попыток. Повторите через пять минут.', 429)

    async def credential(self, conn, uid):
        row = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
            {'key': CREDENTIAL_PREFIX + str(uid)})).first()
        return json.loads(row[0]) if row else None

    async def verified(self, conn, request, actor):
        """An ordinary remembered device session is insufficient to set/reset a password."""
        init = request.headers.get('X-Telegram-Init-Data', '')
        if init:
            return validate_init_data(init, self.api.bot.token, max_age=VERIFY_TTL) == actor['tg_id']
        owner = request.headers.get('X-PhotoBoss-Owner-Launch', '')
        if owner:
            return validate_owner_launch_token(owner, self.api.bot.token) == actor['tg_id']
        saved = await sessions.lookup(conn, sessions.cookie(request), time.time())
        verified = saved.get('telegram_verified_at', 0) if saved else 0
        return bool(saved and saved['telegram_id'] == actor['tg_id']
                    and time.time() - VERIFY_TTL <= verified <= time.time())

    async def status(self, request):
        actor = request['miniapp_actor']
        async with self.api.engine.connect() as conn:
            saved = await self.credential(conn, actor['id'])
        return web.json_response({'configured': bool(saved), 'login': saved['login'] if saved else ''})

    async def setup(self, request):
        self.browser.guard(request)
        actor = request['miniapp_actor']
        await self.rate_limit(request, 'setup:' + str(actor['id']))
        body = await self.api.body(request)
        if set(body) - {'login', 'password', 'currentPassword'}:
            raise AccessError('Лишние параметры настройки входа.', 400)
        login, password = normalize_login(body.get('login')), password_value(body.get('password'))
        hashed = await self.hash_work(password_hash, password)
        async with self.api.engine.begin() as conn:
            # User lock serializes setup, dismissal, login and session renewal.
            await self.browser.actor(conn, actor['tg_id'])
            saved = await self.credential(conn, actor['id'])
            current = body.get('currentPassword')
            allowed = bool(saved and isinstance(current, str) and len(current) <= 128
                           and await self.hash_work(password_matches, current, saved['hash']))
            if not allowed:
                try:
                    allowed = await self.verified(conn, request, actor)
                except AccessError:
                    allowed = False
            if not allowed:
                raise AccessError('Подтвердите аккаунт через Telegram для создания или восстановления пароля.', 403)
            await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:uid) '
                                    'ON CONFLICT(key) DO NOTHING'),
                               {'key': LOGIN_PREFIX + login, 'uid': str(actor['id'])})
            owner = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                                       {'key': LOGIN_PREFIX + login})).scalar_one()
            if owner != str(actor['id']):
                raise AccessError('Этот логин занят. Выберите другой.', 409)
            if saved and saved['login'] != login:
                await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': LOGIN_PREFIX + saved['login']})
            await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) '
                                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                               {'key': CREDENTIAL_PREFIX + str(actor['id']),
                                'value': json.dumps({'login': login, 'hash': hashed})})
            await sessions.revoke_sessions(conn, actor['id'])
            raw = await sessions.issue(conn, actor, '', int(time.time()))
            await self.api.audit_write(conn, actor, 'miniapp_password_changed', 'user', actor['id'],
                                       'Настроен личный вход; предыдущие сеансы отозваны.')
        response = web.json_response({'configured': True, 'login': login})
        sessions.set_cookie(response, raw)
        return response

    async def login(self, request):
        self.browser.guard(request)
        body = await self.api.body(request)
        if set(body) != {'login', 'password'}:
            raise AccessError('Нужны логин и пароль.', 400)
        login = normalize_login(body['login'])
        password = password_value(body['password'])
        await self.rate_limit(request, 'login:' + login)
        async with self.api.engine.connect() as conn:
            uid = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                                      {'key': LOGIN_PREFIX + login})).scalar()
            saved = await self.credential(conn, uid) if uid else None
        valid = await self.hash_work(password_matches, password, saved['hash'] if saved else self.dummy_hash)
        if not saved or not valid:
            raise AccessError('Неверный логин или пароль.', 401)
        async with self.api.engine.begin() as conn:
            old = sessions.cookie(request)
            old_uid = int(old.split('.')[0]) if sessions.session_key(old) else int(uid)
            await conn.execute(text('SELECT id FROM users WHERE id IN (:uid,:old_uid) ORDER BY id FOR UPDATE'),
                               {'uid': int(uid), 'old_uid': old_uid if old_uid < 2**31 else int(uid)})
            row = (await conn.execute(text('SELECT tg_id FROM users WHERE id=:uid FOR UPDATE'),
                                      {'uid': int(uid)})).first()
            if not row:
                raise AccessError('Неверный логин или пароль.', 401)
            actor = await self.browser.actor(conn, row[0])
            latest = await self.credential(conn, uid)
            if latest != saved:
                raise AccessError('Данные входа изменились. Повторите попытку.', 401)
            await self.revoke_device(conn, request)
            raw = await sessions.issue(conn, actor, '', int(time.time()))
        response = web.json_response({'status': 'authenticated'})
        sessions.set_cookie(response, raw)
        return response

    async def revoke_device(self, conn, request):
        raw = sessions.cookie(request)
        key = sessions.session_key(raw)
        if key:
            # Same user lock as issuance prevents a racing renewal restoring this session.
            uid = int(raw.split('.')[0])
            if uid < 2**31:
                await conn.execute(text('SELECT id FROM users WHERE id=:uid FOR UPDATE'), {'uid': uid})
            await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': key})
        pending = request.cookies.get(APPROVAL_COOKIE, '').split('.')[0]
        if re.fullmatch(r'[A-Za-z0-9_-]{24}', pending):
            await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': APPROVAL_PREFIX + pending})

    async def logout(self, request):
        self.browser.guard(request)
        async with self.api.engine.begin() as conn:
            await self.revoke_device(conn, request)
        response = web.json_response({'ok': True})
        for name in (sessions.COOKIE, APPROVAL_COOKIE):
            response.del_cookie(name, path='/', secure=True, httponly=True, samesite='Strict')
        return response

    async def endpoint(self, request):
        try:
            response = await (self.logout(request) if request.path.endswith('/logout') else self.login(request))
        except AccessError as exc:
            response = web.json_response({'error': str(exc)}, status=exc.status)
        except Exception:
            logger.exception('Password authentication unavailable')
            response = web.json_response({'error': 'Вход временно недоступен. Повторите попытку.'}, status=503)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff'})
        return response


def install_password_login(app, api):
    from .launch_policy import app_url
    service = PasswordLogin(api, app_url().split('/app/', 1)[0])
    app.router.add_post('/auth/password/login', service.endpoint)
    app.router.add_post('/auth/password/logout', service.endpoint)
    app.router.add_get('/api/miniapp/account/password', service.status)
    app.router.add_put('/api/miniapp/account/password', service.setup)
