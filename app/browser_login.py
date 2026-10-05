"""Browser-bound Telegram approval; no user IDs or roles accepted from browsers."""
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web
from sqlalchemy import text

from . import miniapp_sessions as sessions
from .miniapp_security import STAFF_ROLES, AccessError

logger = logging.getLogger(__name__)
COOKIE = '__Host-pb_browser_login'
PREFIX = 'miniapp:browser:'
TTL = 300
ID_RE = re.compile(r'^[A-Za-z0-9_-]{24}$')


class BrowserLogin:
    def __init__(self, engine, bot, origin):
        self.engine, self.bot = engine, bot
        parsed = urlsplit(origin)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('Browser login requires a trusted HTTPS origin')
        self.origin = f'https://{parsed.netloc}'

    def guard(self, request):
        if (request.headers.get('X-PhotoBoss-Session') != '1'
                or request.headers.get('Origin') != self.origin
                or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
            raise AccessError('Запрос входа отклонён. Откройте сайт Photo Boss.', 403)

    async def read(self, conn, identifier):
        if not ID_RE.fullmatch(identifier or ''):
            raise AccessError('Запрос входа устарел. Начните вход на сайте заново.', 401)
        row = (await conn.execute(text('SELECT value FROM settings WHERE key=:key FOR UPDATE'),
                                  {'key': PREFIX + identifier})).first()
        try:
            value = json.loads(row[0]) if row else None
            if not value or value['expires'] <= time.time():
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise AccessError('Запрос входа устарел. Начните вход на сайте заново.', 401) from None

    async def write(self, conn, identifier, value):
        await conn.execute(text('UPDATE settings SET value=:value WHERE key=:key'),
                           {'key': PREFIX + identifier, 'value': json.dumps(value)})

    async def actor(self, conn, telegram_id):
        if type(telegram_id) is not int or not 0 < telegram_id < 2**52:
            raise AccessError('Рабочий доступ не назначен.', 403)
        person = (await conn.execute(text('SELECT id,tg_id,active FROM users WHERE tg_id=:tg FOR UPDATE'),
                                     {'tg': telegram_id})).mappings().first()
        if not person or not person['active']:
            raise AccessError('Аккаунт сотрудника не активен. Обратитесь к владельцу.', 403)
        roles = (await conn.execute(text('SELECT role FROM user_roles WHERE user_id=:uid'),
                                    {'uid': person['id']})).scalars().all()
        if not STAFF_ROLES.intersection(roles):
            raise AccessError('Владелец ещё не назначил вам рабочую роль.', 403)
        return dict(person)

    async def start(self, request):
        self.guard(request)
        username = (await self.bot.me()).username
        if not username or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            raise AccessError('Вход временно недоступен. Повторите попытку.', 503)
        now = int(time.time())
        identifier, secret = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        value = {'expires': now + TTL, 'secret': hashlib.sha256(secret.encode()).hexdigest(),
                 'code': f'{secrets.randbelow(1000000):06d}', 'status': 'pending', 'telegram_id': None}
        peer = request.remote or 'unknown'
        fingerprint = hmac.new(self.bot.token.encode(), peer.encode(), hashlib.sha256).hexdigest()
        rate_key = f'miniapp:web-rate:{now // TTL:010d}:{fingerprint}'
        async with self.engine.begin() as conn:
            count = (await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,\'1\') '
                                            'ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(settings.value AS INTEGER)+1 AS TEXT) '
                                            'RETURNING value'), {'key': rate_key})).scalar_one()
            if int(count) > 30:
                # Commit the counter instead of allowing rejected requests to roll it back.
                limited = True
            else:
                limited = False
                await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value)'),
                                   {'key': PREFIX + identifier, 'value': json.dumps(value)})
            # Bounded cleanup, without schema changes or runtime DDL.
            rows = (await conn.execute(text('SELECT key,value FROM settings WHERE key LIKE :prefix LIMIT 200'),
                                       {'prefix': PREFIX + '%'})).all()
            for key, saved in rows:
                try:
                    expired = json.loads(saved)['expires'] <= now
                except (ValueError, KeyError, TypeError):
                    expired = True
                if expired:
                    await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': key})
            await conn.execute(text('DELETE FROM settings WHERE key LIKE :prefix AND key < :cutoff'),
                               {'prefix': 'miniapp:web-rate:%', 'cutoff': f'miniapp:web-rate:{now // TTL - 1:010d}:'})
        if limited:
            raise AccessError('Слишком много попыток. Повторите вход через пять минут.', 429)
        response = web.json_response({'telegramUrl': f'https://t.me/{username}?start=web_{identifier}',
                                      'code': value['code'], 'expiresAt': value['expires']})
        response.set_cookie(COOKIE, identifier + '.' + secret, max_age=TTL, secure=True,
                            httponly=True, samesite='Strict', path='/')
        return response

    async def poll(self, request):
        self.guard(request)
        parts = request.cookies.get(COOKIE, '').split('.')
        async with self.engine.begin() as conn:
            if len(parts) != 2 or not re.fullmatch(r'[A-Za-z0-9_-]{43}', parts[1]):
                return await self.completed(conn, request)
            identifier, secret = parts
            try:
                value = await self.read(conn, identifier)
            except AccessError:
                # Headers/cookie may arrive while the JSON response is lost on a bad network.
                # Recover only through a valid, active device session; never reuse approval.
                return await self.completed(conn, request)
            if not hmac.compare_digest(value['secret'], hashlib.sha256(secret.encode()).hexdigest()):
                raise AccessError('Запрос входа отклонён.', 401)
            if value['status'] == 'rejected':
                raise AccessError('Вход отменён в Telegram.', 403)
            if value['status'] != 'approved':
                return web.json_response({'status': 'pending'})
            actor = await self.actor(conn, value['telegram_id'])
            raw = await sessions.issue(conn, actor, '', int(time.time()))
            if not raw:
                raise AccessError('Рабочий доступ отключён.', 403)
            # Approval and device-session issuance commit together, once only.
            await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': PREFIX + identifier})
        response = web.json_response({'status': 'authenticated'})
        sessions.set_cookie(response, raw)
        response.del_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='Strict')
        return response

    async def completed(self, conn, request):
        value = await sessions.lookup(conn, sessions.cookie(request), time.time())
        if not value:
            raise AccessError('Запрос входа устарел. Начните вход на сайте заново.', 401)
        await self.actor(conn, value['telegram_id'])
        response = web.json_response({'status': 'authenticated'})
        response.del_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='Strict')
        return response

    async def prompt(self, message, identifier):
        if message.chat.type != 'private' or not message.from_user or message.from_user.is_bot:
            return
        async with self.engine.begin() as conn:
            value = await self.read(conn, identifier)
            await self.actor(conn, message.from_user.id)
            if value['status'] != 'pending' or value['telegram_id'] not in (None, message.from_user.id):
                raise AccessError('Этот запрос входа уже обработан. Начните вход на сайте заново.', 403)
            value['telegram_id'] = message.from_user.id
            await self.write(conn, identifier, value)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Подтвердить вход', callback_data='web-login:yes:' + identifier),
            InlineKeyboardButton(text='Отменить', callback_data='web-login:no:' + identifier),
        ]])
        await message.answer(f"Вход на сайт Photo Boss\n{self.origin}\n\nКод на сайте: {value['code']}\n\n"
                             'Подтвердите, только если вы начали вход и видите этот же код на своём сайте. '
                             'Если не начинали вход — нажмите «Отменить». После подтверждения вернитесь в ту же вкладку.',
                             reply_markup=markup, protect_content=True)

    async def approve(self, callback, identifier, accepted):
        if (not callback.message or callback.message.chat.type != 'private'
                or not callback.from_user or callback.from_user.is_bot):
            raise AccessError('Подтверждение доступно только в личном чате.', 403)
        async with self.engine.begin() as conn:
            value = await self.read(conn, identifier)
            await self.actor(conn, callback.from_user.id)
            if value['telegram_id'] != callback.from_user.id or value['status'] != 'pending':
                raise AccessError('Этот запрос входа уже обработан или принадлежит другому пользователю.', 403)
            value['status'] = 'approved' if accepted else 'rejected'
            await self.write(conn, identifier, value)
        await callback.answer('Вход подтверждён' if accepted else 'Вход отменён')
        await callback.message.answer('Вход подтверждён. Вернитесь в ту же вкладку Photo Boss.'
                                       if accepted else 'Вход на сайт отменён.', protect_content=True)

    async def endpoint(self, request):
        try:
            response = await (self.start(request) if request.path.endswith('/start') else self.poll(request))
        except AccessError as exc:
            response = web.json_response({'error': str(exc)}, status=exc.status)
        except Exception:
            logger.exception('Browser login temporarily unavailable')
            response = web.json_response({'error': 'Вход временно недоступен. Повторите попытку.'}, status=503)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff'})
        return response


def service_for_bot(bot):
    from .db import engine
    from .launch_policy import app_url
    return BrowserLogin(engine, bot, app_url().split('/app/', 1)[0])


async def handle_start(message):
    command = (message.text or '').split(maxsplit=1)
    if len(command) != 2 or not command[1].startswith('web_'):
        return False
    try:
        await service_for_bot(message.bot).prompt(message, command[1][4:])
    except AccessError as exc:
        await message.answer(str(exc), protect_content=True)
    return True


async def handle_callback(callback):
    parts = (callback.data or '').split(':')
    if len(parts) != 3 or parts[1] not in {'yes', 'no'}:
        return await callback.answer('Некорректный запрос входа.', show_alert=True)
    try:
        await service_for_bot(callback.bot).approve(callback, parts[2], parts[1] == 'yes')
    except AccessError as exc:
        await callback.answer(str(exc), show_alert=True)


def install_browser_login(app, api):
    from .launch_policy import app_url
    service = BrowserLogin(api.engine, api.bot, app_url().split('/app/', 1)[0])
    app.router.add_post('/auth/browser/start', service.endpoint)
    app.router.add_post('/auth/browser/poll', service.endpoint)
