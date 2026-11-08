"""Minimal employee snapshots and revocable, read-only native widget grants.

No guest contact details, financial mutations, extra tables or bank access.
A grant is tied to an existing browser session and its original roles. Logout,
password reset, dismissal and QR revocation therefore revoke widget access too.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from datetime import UTC, date, datetime, time as clock_time, timedelta

from aiohttp import web
from sqlalchemy import text

from . import miniapp_sessions
from .miniapp_security import AccessError, STAFF_ROLES
from .services.booking_access import booking_scope_sql

GRANTS = 'staff-widget:grant:'
DEVICES = 'staff-widget:device:'
TOKEN = re.compile(r'^w1\.[A-Za-z0-9_-]{43}$')
DEVICE = re.compile(r'^[A-Za-z0-9_-]{16,80}$')
MAX_DEVICES = 10
TTL = 30 * 24 * 60 * 60
DONE = ('SHOT', 'READY_FOR_MANAGER', 'READY_FOR_SALE', 'COMPLETED', 'SOLD')
ROLE_NAMES = {'OWNER': 'Владелец', 'ADMIN': 'Администратор',
              'PHOTOGRAPHER': 'Фотограф', 'MANAGER': 'Менеджер записи'}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def decoded(raw):
    try:
        value = json.loads(raw or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def reply(value, status=200):
    return web.json_response(value, status=status, headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff'})


class StaffWidget:
    def __init__(self, api, *, now=None):
        self.api = api
        self.now = now or (lambda: datetime.now(UTC))

    async def current_actor(self, conn, uid, *, allowed_roles=None):
        row = (await conn.execute(text('SELECT id,tg_id,name,active FROM users WHERE id=:uid'),
                                  {'uid': uid})).mappings().first()
        roles = list((await conn.execute(text('SELECT role FROM user_roles WHERE user_id=:uid'),
                                        {'uid': uid})).scalars()) if row else []
        if not row or not row['active'] or not STAFF_ROLES.intersection(roles):
            raise AccessError('Рабочий доступ отключён.', 401)
        # A changed role requires explicit re-enrolment in the signed-in app.
        if allowed_roles is not None and sorted(roles) != allowed_roles:
            raise AccessError('Откройте Photo Boss, чтобы обновить доступ виджета.', 401)
        return dict(row, roles=roles)

    async def snapshot(self, conn, actor):
        now = self.now().astimezone(UTC)
        local = now.astimezone(self.api.tz)
        day = local.date()
        scope = booking_scope_sql(actor['id'], actor['roles'])
        latest = '(SELECT s.status FROM shootings s WHERE s.booking_id=b.id ORDER BY s.id DESC LIMIT 1)'
        live = "b.status NOT IN ('CANCELLED','REJECTED') AND NOT EXISTS (SELECT 1 FROM settings st WHERE st.key='booking:trash:' || CAST(b.id AS VARCHAR))"
        live += f" AND COALESCE({latest},'') NOT IN ('CANCELLED','REJECTED')"
        params = {'uid': actor['id'], 'day': day, 'until': day + timedelta(days=30)}
        rows = (await conn.execute(text(f'''SELECT b.id,b.shoot_date,b.shoot_time,b.status,
            b.guest_count,h.name AS hotel,h.address,{latest} AS shooting_status
            FROM bookings b JOIN hotels h ON h.id=b.hotel_id WHERE ({scope}) AND {live}
            AND b.shoot_date BETWEEN :day AND :until
            AND b.status NOT IN ('SHOT','READY_FOR_MANAGER','READY_FOR_SALE','COMPLETED','SOLD')
            AND COALESCE({latest},'') NOT IN ('SHOT','READY_FOR_MANAGER','READY_FOR_SALE','COMPLETED','SOLD')
            ORDER BY b.shoot_date,b.shoot_time,b.id LIMIT 101'''), params)).mappings().all()
        counts = (await conn.execute(text(f'''SELECT COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN b.status IN ('SHOT','READY_FOR_MANAGER','READY_FOR_SALE','COMPLETED','SOLD')
            OR {latest} IN ('SHOT','READY_FOR_MANAGER','READY_FOR_SALE','COMPLETED','SOLD') THEN 1 ELSE 0 END),0) AS completed
            FROM bookings b WHERE ({scope}) AND {live} AND b.shoot_date=:day'''), params)).mappings().one()
        next_item = None
        candidates = []
        for row in rows:
            shoot_day = row['shoot_date'] if isinstance(row['shoot_date'], date) else date.fromisoformat(row['shoot_date'])
            shoot_time = row['shoot_time'] if isinstance(row['shoot_time'], clock_time) else clock_time.fromisoformat(row['shoot_time'])
            starts = datetime.combine(shoot_day, shoot_time, tzinfo=self.api.tz)
            # An active shoot is more useful than an unresolved earlier booking.
            candidates.append((0 if row['shooting_status'] == 'SHOOTING' else 1, starts, row))
        if candidates:
            _, starts, row = min(candidates, key=lambda item: (item[0], item[1], item[2]['id']))
            next_item = {'id': row['id'], 'startsAt': starts.astimezone(UTC).isoformat(),
                         'time': starts.strftime('%H:%M'), 'date': starts.date().isoformat(),
                         'hotel': str(row['hotel'])[:200], 'meetingPoint': str(row['address'])[:300] if row['address'] else None,
                         'guestCount': row['guest_count'], 'status': row['shooting_status'] or row['status'],
                         'url': '/app/#shootings?booking=' + str(row['id'])}
        states = []
        for table in ('shift_check_ins', 'shift_check_outs'):
            states.append((await conn.execute(text(f'SELECT status FROM {table} WHERE user_id=:uid AND shift_date=:day ORDER BY id DESC LIMIT 1'), params)).scalar())
        start, end = states
        shift = ('FINISHED' if end == 'FINISHED' else 'REVIEW' if end == 'PENDING_REVIEW' or start == 'PENDING_REVIEW'
                 else 'ON_SHIFT' if start == 'STARTED' else 'OFF_SHIFT')
        role = next((r for r in ('OWNER', 'ADMIN', 'PHOTOGRAPHER', 'MANAGER') if r in actor['roles']), 'MANAGER')
        return {'version': 1, 'generatedAt': now.isoformat(), 'validUntil': (now + timedelta(minutes=15)).isoformat(),
                'timezone': str(self.api.tz), 'employee': {'id': actor['id'], 'name': actor['name'], 'role': ROLE_NAMES[role]},
                'shift': shift, 'nextBooking': next_item,
                'today': {'total': int(counts['total']), 'completed': int(counts['completed'])},
                'truncated': len(rows) > 100, 'appUrl': '/app/#home', 'chatUrl': '/app/#home?workChat=1'}

    async def authenticated_snapshot(self, request):
        async with self.api.engine.connect() as conn:
            actor = await self.current_actor(conn, request['miniapp_actor']['id'])
            return reply(await self.snapshot(conn, actor))

    async def device(self, request):
        body = await self.api.body(request)
        if not isinstance(body, dict) or set(body) != {'deviceId'} or not isinstance(body['deviceId'], str) or not DEVICE.fullmatch(body['deviceId']):
            raise AccessError('Не удалось определить устройство виджета.', 400)
        return digest(body['deviceId'])

    async def enrol(self, request):
        if not getattr(self.api, 'standalone', False):
            raise AccessError('Откройте самостоятельный Photo Boss.', 409)
        device_hash = await self.device(request)
        uid = request['miniapp_actor']['id']
        raw = miniapp_sessions.cookie(request)
        parent_key = miniapp_sessions.session_key(raw)
        now = int(self.now().timestamp())
        async with self.api.engine.begin() as conn:
            # The same user lock as session issuance serializes the device cap.
            lock = '' if conn.dialect.name == 'sqlite' else ' FOR UPDATE'
            await conn.execute(text('SELECT id FROM users WHERE id=:uid' + lock), {'uid': uid})
            actor = await self.current_actor(conn, uid)
            parent = await miniapp_sessions.lookup(conn, raw, now)
            if not parent or not parent_key or not parent_key.startswith(f'miniapp:device:{uid}:') or parent['telegram_id'] != actor['tg_id']:
                raise AccessError('Войдите в Photo Boss перед подключением виджета.', 401)
            prefix = DEVICES + str(uid) + ':'
            index = prefix + device_hash
            devices = (await conn.execute(text('SELECT key,value FROM settings WHERE key LIKE :prefix'), {'prefix': prefix + '%'})).all()
            active = []
            for key, identifier in devices:
                grant = decoded((await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                    {'key': GRANTS + identifier})).scalar())
                expires = grant.get('expires')
                parent = decoded((await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                    {'key': grant.get('parentKey', '')})).scalar())
                parent_expires = parent.get('expires')
                if (grant.get('userId') == uid and grant.get('roles') == sorted(actor['roles'])
                        and type(expires) in (int, float) and math.isfinite(expires) and expires > now
                        and type(parent_expires) in (int, float) and math.isfinite(parent_expires)
                        and parent_expires > now):
                    active.append((key, identifier))
                else:
                    # Only this employee's expired/revoked widget credentials are pruned.
                    await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': key})
                    await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': GRANTS + identifier})
            devices = active
            previous = next((value for key, value in devices if key == index), None)
            if previous is None and len(devices) >= MAX_DEVICES:
                raise AccessError('Уже подключено десять устройств. Отключите ненужный виджет.', 409)
            if previous:
                await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': GRANTS + previous})
            token = 'w1.' + secrets.token_urlsafe(32)
            identifier = digest(token)
            expires = min(now + TTL, int(parent['expires']))
            value = {'userId': uid, 'roles': sorted(actor['roles']), 'parentKey': parent_key,
                     'expires': expires, 'deviceHash': device_hash}
            await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value)'),
                               {'key': GRANTS + identifier, 'value': json.dumps(value)})
            await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                               {'key': index, 'value': identifier})
        return reply({'token': token, 'expiresAt': datetime.fromtimestamp(expires, UTC).isoformat(),
                      'snapshotPath': '/api/staff-widget/v1/snapshot'})

    async def revoke(self, request):
        device_hash = await self.device(request)
        index = DEVICES + str(request['miniapp_actor']['id']) + ':' + device_hash
        async with self.api.engine.begin() as conn:
            identifier = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'), {'key': index})).scalar()
            if identifier:
                await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': GRANTS + identifier})
                await conn.execute(text('DELETE FROM settings WHERE key=:key'), {'key': index})
        return reply({'revoked': True})

    async def bearer_actor(self, conn, token):
        if not TOKEN.fullmatch(token or ''):
            raise AccessError('Откройте Photo Boss, чтобы подключить виджет.', 401)
        raw = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'), {'key': GRANTS + digest(token)})).scalar()
        grant = decoded(raw)
        now = self.now().timestamp()
        uid, expires = grant.get('userId'), grant.get('expires')
        if (type(uid) is not int or uid <= 0 or type(expires) not in (int, float)
                or not math.isfinite(expires) or expires <= now or not isinstance(grant.get('roles'), list)):
            raise AccessError('Откройте Photo Boss, чтобы обновить доступ виджета.', 401)
        parent_key = grant.get('parentKey', '')
        if not re.fullmatch(r'miniapp:device:' + str(uid) + r':[a-f0-9]{64}', parent_key):
            raise AccessError('Доступ виджета завершён.', 401)
        parent = decoded((await conn.execute(text('SELECT value FROM settings WHERE key=:key'), {'key': parent_key})).scalar())
        parent_expires, renewed = parent.get('expires'), parent.get('renewed')
        if (type(parent_expires) not in (int, float) or not math.isfinite(parent_expires) or parent_expires <= now
                or type(renewed) not in (int, float) or not math.isfinite(renewed) or renewed > now):
            raise AccessError('Доступ виджета завершён. Войдите в Photo Boss.', 401)
        actor = await self.current_actor(conn, uid, allowed_roles=grant['roles'])
        if parent.get('telegram_id') != actor['tg_id']:
            raise AccessError('Доступ виджета завершён.', 401)
        method = parent.get('auth_method')
        if method == 'qr':
            from .staff_qr_login import session_grant_roles
            if await session_grant_roles(conn, uid, parent.get('qr_grant_id')) != sorted(actor['roles']):
                raise AccessError('QR изменён. Подключите виджет заново.', 401)
        elif method is not None or 'qr_grant_id' in parent:
            raise AccessError('Доступ виджета завершён.', 401)
        return actor

    async def native_snapshot(self, request):
        try:
            if request.query or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise AccessError('Доступ виджета не подтверждён.', 401)
            authorization = request.headers.get('Authorization', '')
            if not authorization.startswith('Bearer '):
                raise AccessError('Доступ виджета не подтверждён.', 401)
            token = authorization[7:]
            async with self.api.engine.connect() as conn:
                actor = await self.bearer_actor(conn, token)
                return reply(await self.snapshot(conn, actor))
        except AccessError as error:
            return reply({'error': str(error)}, error.status)


def install_staff_widget(app, api):
    if not getattr(api, 'standalone', False):
        return
    service = StaffWidget(api)
    app['staff_widget'] = service
    app.router.add_get('/api/miniapp/widget', service.authenticated_snapshot)
    app.router.add_post('/api/miniapp/widget/grant', service.enrol)
    app.router.add_delete('/api/miniapp/widget/grant', service.revoke)
    app.router.add_get('/api/staff-widget/v1/snapshot', service.native_snapshot)
