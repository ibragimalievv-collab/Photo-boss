"""Persistent, revocable Mini App sessions; only token hashes are stored."""
import hashlib
import json
import re
import secrets
import time

from sqlalchemy import text

from .miniapp_security import AccessError

COOKIE = '__Host-pb_session'
TTL = 30 * 24 * 60 * 60
RENEW_AFTER = 24 * 60 * 60


def cookie(request):
    return getattr(request, 'cookies', {}).get(COOKIE, '')


def session_key(raw):
    if not re.fullmatch(r'[1-9][0-9]{0,15}\.[A-Za-z0-9_-]{43}', raw or ''):
        return None
    return f"miniapp:device:{raw.split('.', 1)[0]}:{hashlib.sha256(raw.encode()).hexdigest()}"


async def revoke_sessions(conn, uid):
    await conn.execute(text('DELETE FROM settings WHERE key LIKE :prefix'),
                       {'prefix': f'miniapp:device:{int(uid)}:%'})


async def lookup(conn, raw, now):
    key = session_key(raw)
    if not key:
        return None
    row = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                              {'key': key})).first()
    if not row:
        return None
    try:
        value = json.loads(row[0])
        if value['expires'] <= now or value['renewed'] > now or type(value['telegram_id']) is not int:
            return None
        return value
    except (ValueError, KeyError, TypeError):
        return None


async def authenticate(engine, request):
    # Custom header + no CORS support prevent cross-site cookie-authenticated requests.
    if (request.headers.get('X-PhotoBoss-Session') != '1'
            or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
        raise AccessError('Откройте Photo Boss внутри Telegram.', 401)
    async with engine.connect() as conn:
        value = await lookup(conn, cookie(request), time.time())
    if not value:
        raise AccessError('Сеанс завершён. Закройте окно и откройте Photo Boss заново в Telegram.', 401)
    return value['telegram_id']


async def refresh(engine, request, response):
    """Issue on successful /me; extend active devices at most once per day."""
    actor = request['miniapp_actor']
    raw = cookie(request)
    now = int(time.time())
    async with engine.begin() as conn:
        value = await lookup(conn, raw, now)
        matching = value and value['telegram_id'] == actor['tg_id'] and raw.split('.', 1)[0] == str(actor['id'])
        if matching and now - value['renewed'] < RENEW_AFTER:
            return
        if not matching and request.path != '/api/miniapp/me':
            return
        # Serialize issuance with dismissal so an in-flight request cannot restore a revoked device.
        user = (await conn.execute(text('SELECT active FROM users WHERE id=:id FOR UPDATE'),
                                  {'id': actor['id']})).first()
        if not user or not user[0]:
            return
        if not matching:
            raw = f"{actor['id']}.{secrets.token_urlsafe(32)}"
        value = {'telegram_id': actor['tg_id'], 'renewed': now, 'expires': now + TTL}
        # Bound stored devices per user and remove expired records during issuance/renewal.
        rows = (await conn.execute(text('SELECT key,value FROM settings WHERE key LIKE :prefix'),
                                   {'prefix': f"miniapp:device:{actor['id']}:%"})).all()
        live = []
        for key, saved in rows:
            try:
                expires = json.loads(saved)['expires']
            except (ValueError, KeyError, TypeError):
                expires = 0
            if expires <= now:
                await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': key})
            elif key != session_key(raw):
                live.append((expires, key))
        for _, key in sorted(live)[:-9]:
            await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': key})
        await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) '
                                'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                           {'key': session_key(raw), 'value': json.dumps(value)})
    response.set_cookie(COOKIE, raw, max_age=TTL, secure=True, httponly=True,
                        samesite='Strict', path='/')
