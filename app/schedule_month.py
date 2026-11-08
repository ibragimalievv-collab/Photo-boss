"""Monthly working/rest-day plans, separate from recorded attendance and shifts.

Photographs of a roster do not establish shift hours or a hotel. Keep those
fields in their existing records; a blank cell is unknown, never a day off.
"""
import calendar
import hashlib
import json
import re
from datetime import date, datetime, timezone

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import AccessError

PREFIX = 'schedule.month:'


def row_key(row):
    value = f"user:{row['userId']}" if row['userId'] is not None else f"name:{row['group']}:{row['name']}"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


async def editable_people(api, conn, actor):
    owner = 'OWNER' in actor['roles']
    if not owner and 'ADMIN' not in actor['roles']:
        return []
    people = await api.rows(conn, """SELECT u.id,u.name,r.role FROM users u
        JOIN user_roles r ON r.user_id=u.id WHERE u.active=TRUE ORDER BY u.name,u.id,r.role""")
    roster = {}
    for person in people:
        row = roster.setdefault(person['id'], {'id': person['id'], 'name': person['name'], 'roles': []})
        row['roles'].append(person['role'])
    allowed = set()
    if not owner:
        memberships = await api.rows(conn, """SELECT e.user_id,e.hotel_id FROM hotel_employees e
            JOIN hotels h ON h.id=e.hotel_id WHERE h.active=TRUE""")
        actor_hotels = {r['hotel_id'] for r in memberships if r['user_id'] == actor['id']}
        allowed = {r['user_id'] for r in memberships if r['hotel_id'] in actor_hotels}
    return [p for p in roster.values() if set(p['roles']) & {'MANAGER', 'PHOTOGRAPHER'}
            and (owner or p['id'] in allowed and 'OWNER' not in p['roles'])]


async def load_plan(api, conn, month):
    stored = await api.rows(conn, 'SELECT value FROM settings WHERE key=:key', key=PREFIX + month)
    if not stored:
        return None
    plan = validate_plan(json.loads(stored[0]['value']))
    if plan['month'] != month:
        raise ValueError('Month mismatch')
    return plan


async def lock_month(conn, month):
    if conn.dialect.name == 'postgresql':
        await conn.execute(text('SELECT pg_advisory_xact_lock(1346522959, :month)'),
                           {'month': int(month.replace('-', ''))})


async def store_plan(conn, plan):
    await conn.execute(text('''INSERT INTO settings(key,value) VALUES (:key,:value)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value'''),
        {'key': PREFIX + plan['month'], 'value': json.dumps(plan, ensure_ascii=False, sort_keys=True)})


async def mark_working_day(api, conn, actor, uid, day):
    """Caller holds user locks, then sorted month locks. No historical import."""
    month = str(day)[:7]
    plan = await load_plan(api, conn, month)
    if plan is None:
        return
    person = next((r for r in plan['rows'] if r['userId'] == uid), None)
    if person is None:
        return
    i = int(str(day)[-2:])-1
    before = person['days'][i]
    if before == 'R':
        return
    person['days'] = person['days'][:i]+'R'+person['days'][i+1:]
    await store_plan(conn, plan)
    await api.audit_write(conn, actor, 'schedule_month_days_updated', 'schedule', None,
        json.dumps({'month': month, 'employeeId': uid, 'date': str(day), 'before': before,
                    'after': 'R', 'reason': 'Сохранены часы смены', 'attendanceChanged': False}, ensure_ascii=False))


def month_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'20\d{2}-(?:0[1-9]|1[0-2])', value):
        raise AccessError('Выберите месяц графика.', 400)
    return value


def validate_plan(raw):
    if not isinstance(raw, dict) or set(raw) != {'month', 'rows', 'source'}:
        raise AccessError('Проверьте график месяца.', 400)
    month = month_value(raw['month'])
    length = calendar.monthrange(*map(int, month.split('-')))[1]
    if not isinstance(raw['source'], str) or not 1 <= len(raw['source']) <= 150:
        raise AccessError('Укажите источник графика.', 400)
    if not isinstance(raw['rows'], list) or not 1 <= len(raw['rows']) <= 100:
        raise AccessError('Добавьте сотрудников в график.', 400)
    rows, names, uids = [], set(), set()
    for item in raw['rows']:
        if (not isinstance(item, dict) or set(item) != {'name', 'group', 'userId', 'days'}
                or not isinstance(item['name'], str) or not 1 <= len(item['name'].strip()) <= 150
                or any(ord(c) < 32 for c in item['name'])
                or not isinstance(item['group'], str) or item['group'] not in {'MANAGER', 'PHOTOGRAPHER'}
                or item['userId'] is not None and (type(item['userId']) is not int or not 0 < item['userId'] < 2**31)
                or not isinstance(item['days'], str) or len(item['days']) != length
                or set(item['days']) - {'R', 'V', '.'}):
            raise AccessError('Проверьте сотрудника и все дни месяца: Р — работа, В — выходной, точка — не указано.', 400)
        label = item['name'].strip()
        if label.casefold() in names or item['userId'] is not None and item['userId'] in uids:
            raise AccessError('Сотрудник повторяется в графике.', 400)
        names.add(label.casefold())
        if item['userId'] is not None:
            uids.add(item['userId'])
        rows.append({**item, 'name': label})
    return {'month': month, 'source': raw['source'].strip(), 'rows': rows}


def revision(plan):
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()[:24]


async def month_plan(api, conn, actor, month):
    month = month_value(month)
    owner = 'OWNER' in actor['roles']
    people = await editable_people(api, conn, actor)
    editable = {p['id'] for p in people}
    base = {'actorId': actor['id'], 'month': month, 'rows': [], 'source': '', 'revision': None,
            'canEdit': owner or bool(editable), 'employees': people}
    try:
        plan = await load_plan(api, conn, month)
        if plan is None:
            return base
    except (ValueError, TypeError, KeyError, AccessError):
        return {**base, 'canEdit': False,
                'error': 'Не удалось прочитать месячный график. Смены и отметки сохранены.'}
    visible = [dict(row, key=row_key(row), editable=owner or row['userId'] in editable)
               for row in plan['rows'] if owner or row['userId'] in editable or row['userId'] == actor['id']]
    return {**base, **plan, 'rows': visible, 'revision': revision(plan) if base['canEdit'] else None}


class ScheduleMonth:
    def __init__(self, api):
        self.api = api

    async def get(self, request):
        actor = request['miniapp_actor']
        month = month_value(request.query.get('month', str(self.api.today())[:7]))
        async with self.api.engine.connect() as conn:
            result = await month_plan(self.api, conn, actor, month)
        return web.json_response(result, headers={'Cache-Control': 'no-store'})

    async def patch(self, request):
        actor = request['miniapp_actor']
        if not set(actor['roles']) & {'OWNER', 'ADMIN'}:
            raise AccessError('Изменять график могут владелец и администратор.', 403)
        body = await self.api.body(request, max_bytes=524288)
        if set(body) != {'month', 'changes'} or not isinstance(body['changes'], list) or not 1 <= len(body['changes']) <= 3100:
            raise AccessError('Выберите дни, которые нужно изменить.', 400)
        month = month_value(body['month'])
        seen = set()
        for change in body['changes']:
            try:
                if not isinstance(change, dict) or set(change) != {'rowKey', 'date', 'previous', 'status'}:
                    raise ValueError
                day = date.fromisoformat(change['date'])
                if (str(day)[:7] != month or not isinstance(change['rowKey'], str)
                        or not re.fullmatch('[a-f0-9]{24}', change['rowKey'])
                        or change['previous'] not in {'R', 'V', '.'} or change['status'] not in {'R', 'V', '.'}
                        or (change['rowKey'], day) in seen):
                    raise ValueError
                seen.add((change['rowKey'], day))
            except (TypeError, ValueError, KeyError):
                raise AccessError('Проверьте даты и выбранные дни графика.', 400) from None
        async with self.api.engine.begin() as conn:
            from .people import People
            roles = await People(self.api).current_editor(conn, actor['id'])
            current = dict(actor, roles=sorted(roles))
            await lock_month(conn, month)
            plan = await load_plan(self.api, conn, month)
            if plan is None:
                raise AccessError('Сначала добавьте сотрудника в график.', 409)
            allowed = {p['id'] for p in await editable_people(self.api, conn, current)}
            by_key = {row_key(row): row for row in plan['rows']}
            changed, unchanged = [], 0
            for change in body['changes']:
                person = by_key.get(change['rowKey'])
                if person is None:
                    raise AccessError('Строка графика изменилась. Обновите месяц.', 409)
                if 'OWNER' not in roles and person['userId'] not in allowed:
                    raise AccessError('Нет доступа к графику этого сотрудника.', 403)
                index = int(change['date'][-2:])-1
                before = person['days'][index]
                if before == change['status']:
                    unchanged += 1
                    continue
                if before != change['previous']:
                    raise AccessError('Этот день уже изменён другим руководителем. Обновите график и проверьте изменение.', 409)
                if change['status'] == 'V' and person['userId'] is not None:
                    from .miniapp_api import as_utc
                    plans = await self.api.rows(conn,
                        "SELECT start_at FROM shifts WHERE user_id=:uid AND status='PLANNED'",
                        uid=person['userId'])
                    if any(str(as_utc(s['start_at']).astimezone(self.api.tz).date()) == change['date']
                           and as_utc(s['start_at']) > datetime.now(timezone.utc) for s in plans):
                        raise AccessError('На этот день уже назначена смена с часами. Сначала отмените её через «Отель и часы смены», затем отметьте выходной.', 409)
                person['days'] = person['days'][:index] + change['status'] + person['days'][index+1:]
                changed.append({'employeeId': person['userId'], 'employee': person['name'],
                                'date': change['date'], 'before': before, 'after': change['status']})
            if changed:
                await store_plan(conn, plan)
                await self.api.audit_write(conn, current, 'schedule_month_days_updated', 'schedule', None,
                    json.dumps({'month': month, 'changes': changed, 'attendanceChanged': False,
                                'shiftHoursChanged': False}, ensure_ascii=False))
            result = await month_plan(self.api, conn, current, month)
        return web.json_response({'ok': True, 'changed': len(changed), 'unchanged': unchanged, 'plan': result})

    async def add_person(self, request):
        actor = request['miniapp_actor']
        if not set(actor['roles']) & {'OWNER', 'ADMIN'}:
            raise AccessError('Добавлять сотрудников в график могут владелец и администратор.', 403)
        body = await self.api.body(request)
        if (set(body) != {'month', 'userId', 'group'} or type(body['userId']) is not int
                or not isinstance(body['group'], str) or body['group'] not in {'MANAGER', 'PHOTOGRAPHER'}):
            raise AccessError('Выберите сотрудника и рабочую роль.', 400)
        month = month_value(body['month'])
        async with self.api.engine.begin() as conn:
            from .people import People
            roles = await People(self.api).current_editor(conn, actor['id'])
            current = dict(actor, roles=sorted(roles))
            person = next((p for p in await editable_people(self.api, conn, current) if p['id'] == body['userId']), None)
            if person is None or body['group'] not in person['roles']:
                raise AccessError('Сотрудник или рабочая роль недоступны.', 403)
            await lock_month(conn, month)
            plan = await load_plan(self.api, conn, month) or {'month': month, 'source': 'График команды', 'rows': []}
            if not any(row['userId'] == person['id'] for row in plan['rows']):
                length = calendar.monthrange(*map(int, month.split('-')))[1]
                plan['rows'].append({'name': person['name'], 'userId': person['id'], 'group': body['group'], 'days': '.'*length})
                validate_plan(plan)
                await store_plan(conn, plan)
                await self.api.audit_write(conn, current, 'schedule_month_employee_added', 'schedule', person['id'],
                    json.dumps({'month': month, 'employeeId': person['id'], 'group': body['group']}, ensure_ascii=False))
            result = await month_plan(self.api, conn, current, month)
        return web.json_response({'ok': True, 'plan': result})

    async def put(self, request):
        actor = request['miniapp_actor']
        if 'OWNER' not in actor['roles']:
            raise AccessError('Менять месячный график может владелец.', 403)
        body = await self.api.body(request)
        if set(body) != {'plan', 'revision'} or body['revision'] is not None and not isinstance(body['revision'], str):
            raise AccessError('Сначала обновите график.', 400)
        plan = validate_plan(body['plan'])
        async with self.api.engine.begin() as conn:
            from .people import People
            await People(self.api).current_editor(conn, actor['id'])
            owner = await self.api.rows(conn, "SELECT role FROM user_roles WHERE user_id=:uid AND role='OWNER'", uid=actor['id'])
            if not owner:
                raise AccessError('Право владельца больше не действует.', 403)
            if conn.dialect.name == 'postgresql':
                await conn.execute(text('SELECT pg_advisory_xact_lock(1346522959, :month)'),
                                   {'month': int(plan['month'].replace('-', ''))})
            for uid in sorted({row['userId'] for row in plan['rows'] if row['userId'] is not None}):
                people = await self.api.rows(conn, 'SELECT id FROM users WHERE id=:uid AND active=TRUE', uid=uid)
                if not people:
                    raise AccessError('В графике есть недоступный сотрудник. Его строку можно сохранить без привязки к аккаунту.', 400)
            old = await month_plan(self.api, conn, actor, plan['month'])
            if old['revision'] == revision(plan):
                return web.json_response({'ok': True, 'revision': old['revision'], 'unchanged': True})
            if old.get('error') or body['revision'] != old['revision']:
                raise AccessError('График уже изменён. Обновите его перед сохранением.', 409)
            await conn.execute(text('''INSERT INTO settings(key,value) VALUES (:key,:value)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value'''),
                {'key': PREFIX + plan['month'], 'value': json.dumps(plan, ensure_ascii=False, sort_keys=True)})
            await self.api.audit_write(conn, actor, 'schedule_month_updated', 'schedule', None,
                json.dumps({'month': plan['month'], 'beforeRevision': old['revision'],
                            'afterRevision': revision(plan), 'employees': len(plan['rows']),
                            'workingDays': sum(row['days'].count('R') for row in plan['rows']),
                            'daysOff': sum(row['days'].count('V') for row in plan['rows']),
                            'attendanceChanged': False, 'shiftHoursChanged': False}, ensure_ascii=False))
        return web.json_response({'ok': True, 'revision': revision(plan)})


def install_schedule_month(app, api):
    feature = ScheduleMonth(api)
    app.router.add_get('/api/miniapp/schedule/month', feature.get)
    app.router.add_put('/api/miniapp/schedule/month', feature.put)
    app.router.add_patch('/api/miniapp/schedule/month', feature.patch)
    app.router.add_post('/api/miniapp/schedule/month/employees', feature.add_person)
