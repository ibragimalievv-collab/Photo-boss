"""Persistent, revocable Mini App sessions; only token hashes are stored."""
import hashlib
import json
import math
import re
import secrets
import time

from sqlalchemy import text

from .miniapp_security import AccessError

COOKIE = '__Host-pb_session'
TTL = 30 * 24 * 60 * 60
RENEW_AFTER = 24 * 60 * 60
MIGRATION_PRESERVE_PREFIX = 'miniapp:migration-device-preserve:'


def migration_preserve_keys(saved, uid, now):
    """Read a bounded import-only pruning exception; never an auth credential.

    The migration helper may seed this only in the new database. Its fixed
    deadline is the last original expiry among imported devices; renewal never
    extends the flag. Normal row expiry and revocation remain authoritative.
    """
    if type(uid) is not int or uid <= 0 or not isinstance(saved, str) or len(saved) > 8192:
        return set()
    try:
        value = json.loads(saved)
        if (not isinstance(value, dict) or set(value) != {'version', 'keys', 'until'}
                or type(value['version']) is not int or value['version'] != 1
                or type(value['until']) not in (int, float) or not math.isfinite(value['until'])
                or not now < value['until'] <= now + TTL
                or not isinstance(value['keys'], list) or len(value['keys']) > 32):
            return set()
        pattern = re.compile(r'miniapp:device:' + str(uid) + r':[a-f0-9]{64}')
        keys = value['keys']
        if any(not isinstance(key, str) or not pattern.fullmatch(key) for key in keys):
            return set()
        if len(set(keys)) != len(keys):
            return set()
        return set(keys)
    except (ValueError, KeyError, TypeError, OverflowError):
        return set()


async def preserved_imported_keys(conn, uid, now):
    row = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
        {'key': MIGRATION_PRESERVE_PREFIX + str(uid)})).first()
    return migration_preserve_keys(row[0], uid, now) if row else set()


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
        method = value.get('auth_method')
        if method == 'qr':
            from .staff_qr_login import session_grant_roles
            uid = int(raw.split('.', 1)[0])
            roles = await session_grant_roles(conn, uid, value.get('qr_grant_id'))
            if roles is None:
                return None
            value['qr_roles'] = roles
        elif method is not None or 'qr_grant_id' in value:
            return None
        return value
    except (ValueError, KeyError, TypeError):
        return None


async def authenticate(engine, request, *, standalone=False):
    # Custom header + no CORS support prevent cross-site cookie-authenticated requests.
    if (request.headers.get('X-PhotoBoss-Session') != '1'
            or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
        raise AccessError('Войдите в Photo Boss по QR-коду или паролю.' if standalone else 'Откройте Photo Boss внутри Telegram.', 401)
    async with engine.connect() as conn:
        value = await lookup(conn, cookie(request), time.time())
    if not value:
        raise AccessError('Сеанс завершён. Войдите снова по QR-коду или паролю.' if standalone else 'Сеанс завершён. Закройте окно и откройте Photo Boss заново в Telegram.', 401)
    request['miniapp_auth_method'] = value.get('auth_method', 'session')
    if value.get('auth_method') == 'qr':
        request['miniapp_qr_roles'] = value['qr_roles']
    return value['telegram_id']


async def refresh(engine, request, response):
    """Issue on successful /me; extend active devices at most once per day."""
    actor = request['miniapp_actor']
    raw = cookie(request)
    now = int(time.time())
    async with engine.begin() as conn:
        value = await lookup(conn, raw, now)
        matching = value and value['telegram_id'] == actor['tg_id'] and raw.split('.', 1)[0] == str(actor['id'])
        if raw and session_key(raw) and raw.split('.', 1)[0] == str(actor['id']) and not value:
            # An in-flight signed /me must not replace a device revoked by logout/reset.
            return
        if matching and now - value['renewed'] < RENEW_AFTER:
            return
        if not matching and request.path != '/api/miniapp/me':
            return
        raw = await issue(conn, actor, raw if matching else '', now)
    if raw:
        set_cookie(response, raw)


async def issue(conn, actor, raw, now, *, telegram_verified_at=None, qr_grant_id=None):
    """Issue a device session in the caller's transaction; actor is server-verified."""
    # Serialize issuance with dismissal so an in-flight request cannot restore a revoked device.
    lock = '' if getattr(getattr(conn, 'dialect', None), 'name', None) == 'sqlite' else ' FOR UPDATE'
    user = (await conn.execute(text('SELECT active FROM users WHERE id=:id' + lock),
                              {'id': actor['id']})).first()
    if not user or not user[0]:
        return
    previous = await lookup(conn, raw, now) if raw else None
    if raw and not previous:
        # Logout or password reset may have revoked it while refresh waited for the lock.
        return
    qr_id = qr_grant_id or (previous or {}).get('qr_grant_id')
    if qr_id:
        from .staff_qr_login import valid_session_grant
        # A QR login creates a fresh device; renewal only preserves its
        # existing provenance. An ordinary session cannot be silently changed.
        if ((qr_grant_id is not None and raw)
                or not await valid_session_grant(conn, actor['id'], qr_id)):
            return
    if not raw:
        raw = f"{actor['id']}.{secrets.token_urlsafe(32)}"
    value = {'telegram_id': actor['tg_id'], 'renewed': now, 'expires': now + TTL}
    verified_at = telegram_verified_at or (previous or {}).get('telegram_verified_at')
    if qr_id:
        value.update(auth_method='qr', qr_grant_id=qr_id)
        # Only a new QR authentication verifies possession. Daily cookie renewal
        # must never turn an old remembered device into a recent verification.
        verified_at = now if qr_grant_id is not None else (previous or {}).get('qr_verified_at')
        if verified_at is not None:
            value['qr_verified_at'] = verified_at
    elif verified_at:
        value['telegram_verified_at'] = verified_at
    # Normal cap stays ten. A short import-only exception protects exact old
    # devices while allowing only bounded post-migration additions. The flag
    # neither creates nor authenticates a device and is never written here.
    preserved = await preserved_imported_keys(conn, actor['id'], now)
    rows = (await conn.execute(text('SELECT key,value FROM settings WHERE key LIKE :prefix'),
                               {'prefix': f"miniapp:device:{actor['id']}:%"})).all()
    live, protected_live = [], set()
    current_key = session_key(raw)
    for key, saved in rows:
        try:
            expires = json.loads(saved)['expires']
            if type(expires) not in (int, float) or not math.isfinite(expires):
                expires = 0
        except (ValueError, KeyError, TypeError, OverflowError):
            expires = 0
        if expires <= now:
            await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': key})
        else:
            if key in preserved:
                protected_live.add(key)
            elif key != current_key:
                live.append((expires, key))
    # A new ordinary device counts inside this allowance. Renewal of an old
    # protected device must not evict the one post-migration phone unnecessarily.
    unprotected_cap = max(1, 10 - len(protected_live))
    slots = unprotected_cap - (0 if current_key in protected_live else 1)
    for _, key in sorted(live)[:max(0, len(live) - slots)]:
        await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': key})
    await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) '
                            'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                       {'key': session_key(raw), 'value': json.dumps(value)})
    return raw


def set_cookie(response, raw):
    response.set_cookie(COOKIE, raw, max_age=TTL, secure=True, httponly=True,
                        samesite='Strict', path='/')

