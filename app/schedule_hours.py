"""Edit an existing future plan without replacing its ID or attendance history."""
import json
from datetime import date, datetime, timezone

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import AccessError, parse_shift, require_schedule_editor
from .miniapp_api import as_utc


def shift_values(shift, tz):
    start = as_utc(shift['start_at']).astimezone(tz)
    end = as_utc(shift['end_at']).astimezone(tz)
    return {'hotelId': shift['hotel_id'], 'date': str(start.date()),
            'start': start.strftime('%H:%M'), 'end': end.strftime('%H:%M')}


async def edit_shift(api, request):
    actor = request['miniapp_actor']
    require_schedule_editor(actor['roles'])
    try:
        sid = int(request.match_info['id'])
        if not 0 < sid < 2**31:
            raise ValueError
    except ValueError:
        raise AccessError('Некорректный номер смены.', 400) from None
    body = await api.body(request)
    if (set(body) != {'hotelId', 'date', 'start', 'end', 'previous'}
            or not isinstance(body['previous'], dict)
            or set(body['previous']) != {'hotelId', 'date', 'start', 'end'}
            or type(body['previous']['hotelId']) is not int
            or not all(isinstance(body['previous'][k], str) for k in ('date', 'start', 'end'))):
        raise AccessError('Обновите смену и проверьте её часы.', 400)
    async with api.engine.begin() as conn:
        rows = await api.rows(conn, 'SELECT * FROM shifts WHERE id=:id', id=sid)
        if not rows:
            raise AccessError('Смена не найдена.', 404)
        uid = rows[0]['user_id']
        from .people import People
        roles = await People(api).current_editor(conn, actor['id'], uid)
        require_schedule_editor(roles)
        people = await api.rows(conn, 'SELECT active FROM users WHERE id=:uid', uid=uid)
        target_roles = {r['role'] for r in await api.rows(conn, 'SELECT role FROM user_roles WHERE user_id=:uid', uid=uid)}
        if not people or not people[0]['active'] or not target_roles & {'MANAGER', 'PHOTOGRAPHER'}:
            raise AccessError('Сотрудник недоступен для планирования.', 400)
        if 'OWNER' not in roles and 'OWNER' in target_roles:
            raise AccessError('Изменить смену владельца может только владелец.', 403)
        desired = {k: body[k] for k in ('hotelId', 'date', 'start', 'end')}
        _, hid, start, end = parse_shift(dict(desired, userId=uid), api.tz, api.today())
        from .schedule_month import lock_month, mark_working_day
        for month in sorted({body['previous']['date'][:7], body['date'][:7]}):
            from .schedule_month import month_value
            await lock_month(conn, month_value(month))
        # Same user locks/order as batch planning and attendance. A stale editor
        # cannot overwrite another administrator's update after it gets the lock.
        rows = await api.rows(conn, 'SELECT * FROM shifts WHERE id=:id FOR UPDATE', id=sid)
        shift = rows[0]
        before = shift_values(shift, api.tz)
        for hotel_id in {hid, shift['hotel_id']}:
            if 'OWNER' not in roles and not await api.rows(conn,
                    'SELECT hotel_id FROM hotel_employees WHERE user_id=:uid AND hotel_id=:hid',
                    uid=actor['id'], hid=hotel_id):
                raise AccessError('Изменять смену можно только в назначенных вам отелях.', 403)
        if not await api.rows(conn, 'SELECT id FROM hotels WHERE id=:hid AND active=TRUE', hid=hid):
            raise AccessError('Отель недоступен.', 400)
        canonical = dict(desired, hotelId=hid)
        if shift['status'] == 'PLANNED' and before == canonical:
            return web.json_response({'ok': True, 'id': sid, 'unchanged': True})
        if before != body['previous']:
            raise AccessError('Смена уже изменена. Обновите график перед сохранением.', 409)
        now = datetime.now(timezone.utc)
        if (shift['status'] != 'PLANNED' or as_utc(shift['start_at']) <= now
                or as_utc(start) <= now or await api.rows(conn,
                'SELECT id FROM shift_check_ins WHERE user_id=:uid AND shift_date=:day AND started_at IS NOT NULL',
                uid=uid, day=date.fromisoformat(before['date']))):
            raise AccessError('Начавшуюся или прошедшую смену изменять нельзя. Фактические отметки сохранены.', 409)
        if await api.rows(conn, """SELECT id FROM shifts WHERE user_id=:uid AND id<>:id
                AND status<>'CANCELLED' AND start_at<:end AND end_at>:start""",
                uid=uid, id=sid, start=start, end=end):
            raise AccessError('Часы пересекаются с другой сменой сотрудника.', 409)
        await conn.execute(text('UPDATE shifts SET hotel_id=:hid,start_at=:start,end_at=:end WHERE id=:id'),
                           {'hid': hid, 'start': start, 'end': end, 'id': sid})
        await api.audit_write(conn, dict(actor, roles=sorted(roles)), 'miniapp_shift_updated', 'shift', sid,
                              json.dumps({'employeeId': uid, 'before': before, 'after': canonical}, ensure_ascii=False))
        await mark_working_day(api, conn, actor, uid, desired['date'])
    return web.json_response({'ok': True, 'id': sid})


def install_schedule_hours(app, api):
    async def patch(request):
        return await edit_shift(api, request)
    app.router.add_patch('/api/miniapp/schedule/{id}', patch)
