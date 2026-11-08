"""Working day rule: everyone starts at 09:00 and works until the last sale.

Plans are stored as 09:00–23:59 local time. 23:59 marks an open end: there is
no planned leaving time, so leaving "early" does not exist for such a shift;
the employee closes the shift in the app after the last sale.

Shifts planned before the rule keep their old hours until an editor moves
them, in one tap, with POST /api/miniapp/schedule/normalize-hours.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import AccessError, require_schedule_editor

WORK_START = '09:00'
OPEN_END = '23:59'
# Future plans further out than this are not touched (and not counted).
HORIZON_DAYS = 120


def is_open_end(local_end):
    """local_end: an aware datetime in the business time zone."""
    return local_end is not None and local_end.strftime('%H:%M') >= OPEN_END


def working_day(day, tz):
    """UTC-naive bounds of a working day, stored exactly as parse_shift stores them."""
    start = datetime.fromisoformat(f'{day}T{WORK_START}').replace(tzinfo=tz)
    end = datetime.fromisoformat(f'{day}T{OPEN_END}').replace(tzinfo=tz)
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def legacy_shifts(api, conn, actor_id, roles, *, lock=False):
    """Planned shifts from tomorrow on that still carry hours from before the
    rule, in the editor's hotels.

    Today is never touched: its plan may already drive arrival fines. A person
    with two shifts on one day is reported, not merged: one of them has to be
    cancelled first (one hotel per day). Shifts in other hotels still count for
    that check, so an administrator cannot create an overlap they cannot see.
    """
    first = api.today() + timedelta(days=1)
    lo = datetime.combine(first, time.min, api.tz).astimezone(timezone.utc).replace(tzinfo=None)
    hi = lo + timedelta(days=HORIZON_DAYS)
    rows = await api.rows(conn, """SELECT id,user_id,hotel_id,start_at,end_at,status FROM shifts
        WHERE status<>'CANCELLED' AND start_at>=:lo AND start_at<:hi
        ORDER BY user_id,start_at,id""" + (' FOR UPDATE' if lock else ''), lo=lo, hi=hi)
    allowed = None
    if 'OWNER' not in roles:
        allowed = {r['hotel_id'] for r in await api.rows(conn,
            'SELECT hotel_id FROM hotel_employees WHERE user_id=:uid', uid=actor_id)}
    days = {}
    for row in rows:
        start, end = _utc(row['start_at']).astimezone(api.tz), _utc(row['end_at']).astimezone(api.tz)
        days.setdefault((row['user_id'], start.date()), []).append(
            {'id': row['id'], 'userId': row['user_id'], 'hotelId': row['hotel_id'], 'status': row['status'],
             'date': start.date(), 'start': start.strftime('%H:%M'), 'end': end.strftime('%H:%M'),
             'sameDay': end.date() == start.date()})
    candidates, conflicts = [], []
    for (_uid, _day), shifts in sorted(days.items(), key=lambda item: (item[0][1], item[0][0])):
        visible = [s for s in shifts if allowed is None or s['hotelId'] in allowed]
        if not visible:
            continue
        if len(shifts) > 1:
            conflicts.append({'userId': _uid, 'date': str(_day)})
            continue
        shift = shifts[0]
        legacy = (shift['start'], shift['end'], shift['sameDay']) != (WORK_START, OPEN_END, True)
        if shift['status'] == 'PLANNED' and legacy:
            candidates.append(shift)
    return {'candidates': candidates, 'conflicts': conflicts}


def summary(found):
    days = [s['date'] for s in found['candidates']]
    return {'count': len(days), 'from': str(min(days)) if days else None, 'to': str(max(days)) if days else None,
            'conflicts': found['conflicts'][:20], 'conflictCount': len(found['conflicts'])}


async def legacy_summary(api, conn, actor):
    if not actor['permissions'].get('manageSchedule'):
        return None
    return summary(await legacy_shifts(api, conn, actor['id'], set(actor['roles'])))


class ShiftHours:
    def __init__(self, api):
        self.api = api

    async def normalize(self, request):
        actor = request['miniapp_actor']
        require_schedule_editor(actor['roles'])
        body = await self.api.body(request)
        if body != {'confirm': True}:
            raise AccessError('Подтвердите перевод смен.', 400)
        from .schedule_import import day_key, put
        from .schedule_month import lock_month
        async with self.api.engine.begin() as conn:
            users = await self.api.rows(conn, 'SELECT id,active FROM users WHERE id=:uid FOR UPDATE', uid=actor['id'])
            if not users or not users[0]['active']:
                raise AccessError('Рабочий доступ отключён.', 403)
            roles = {r['role'] for r in await self.api.rows(conn,
                'SELECT role FROM user_roles WHERE user_id=:uid', uid=actor['id'])}
            require_schedule_editor(roles)
            # Same month lock order as manual planning and imports.
            first = self.api.today() + timedelta(days=1)
            months, day = [], first.replace(day=1)
            while day <= first + timedelta(days=HORIZON_DAYS):
                months.append(f'{day:%Y-%m}')
                day = (day + timedelta(days=32)).replace(day=1)
            for month in months:
                await lock_month(conn, month)
            found = await legacy_shifts(self.api, conn, actor['id'], roles, lock=True)
            for shift in found['candidates']:
                start, end = working_day(shift['date'], self.api.tz)
                await conn.execute(text("UPDATE shifts SET start_at=:start,end_at=:end WHERE id=:id AND status='PLANNED'"),
                                   {'start': start, 'end': end, 'id': shift['id']})
                # Keep an imported plan for that day attached to the shift (its role
                # label), instead of leaving it behind as a second "plan only" row.
                key = day_key(shift['userId'], str(shift['date']))
                stored = await self.api.rows(conn, 'SELECT value FROM settings WHERE key=:key', key=key)
                if stored:
                    plan = json.loads(stored[0]['value'])
                    if (plan.get('role') != 'OFF' and plan.get('hotelId') == shift['hotelId']
                            and (plan.get('start'), plan.get('end')) == (shift['start'], shift['end'])):
                        await put(conn, key, plan | {'start': WORK_START, 'end': OPEN_END})
                await self.api.audit_write(conn, actor, 'miniapp_shift_hours_normalized', 'shift', shift['id'],
                    f"Сотрудник №{shift['userId']} · {shift['date']} · отель №{shift['hotelId']} · "
                    f"{shift['start']}–{shift['end']} → с {WORK_START} до последней продажи")
        result = summary(found)
        return web.json_response({'changed': result['count'], 'from': result['from'], 'to': result['to'],
                                  'conflicts': result['conflicts'], 'conflictCount': result['conflictCount']})


def install_shift_hours(app, api):
    app.router.add_post('/api/miniapp/schedule/normalize-hours', ShiftHours(api).normalize)
