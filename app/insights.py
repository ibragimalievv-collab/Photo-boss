"""Owner control center over existing ledgers."""
import json
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import AccessError, financial_period, require_owner
from .models import utc_now
from .services.insights import compare_periods, parsed, sync_events


class Insights:
    def __init__(self, api):
        self.api = api

    async def report(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        period = request.query.get('period', 'week')
        start, end = financial_period(actor['roles'], period, self.api.today(), start=request.query.get('from'), end=request.query.get('to'))
        async with self.api.engine.connect() as conn:
            result = await compare_periods(self.api, conn, start, end, offset_days=7 if period=='week' else None)
        return web.json_response(result)

    async def events(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        async with self.api.engine.connect() as conn:
            rows = await self.api.rows(conn, '''SELECT * FROM notifications WHERE user_id=:uid
                AND kind IN ('control','daily_summary') ORDER BY id DESC LIMIT 200''', uid=actor['id'])
        return web.json_response({'items': [{'id': r['id'], 'title': r['text'], 'priority': r['priority'],
            'kind': r['kind'], 'at': str(r['created_at']), 'acknowledged': bool(r['acknowledged_at']),
            'resolved': bool(r['resolved_at']), 'data': parsed(r['payload'])} for r in rows]})

    async def scan(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        if await self.api.body(request):
            raise AccessError('Лишние параметры.', 400)
        async with self.api.engine.begin() as conn:
            await sync_events(self.api, conn, actor['id'])
        return await self.events(request)

    async def acknowledge(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        from .people import positive_id
        eid = positive_id(request.match_info['id'])
        async with self.api.engine.begin() as conn:
            changed = await self.api.rows(conn, '''UPDATE notifications SET acknowledged_at=:now
                WHERE id=:id AND user_id=:uid AND acknowledged_at IS NULL RETURNING id''',
                now=utc_now(), id=eid, uid=actor['id'])
            if changed:
                await self.api.audit_write(conn, actor, 'event_acknowledged', 'notification', eid, 'Событие просмотрено; исходные данные не изменены.')
        return web.json_response({'ok': True})


def install_insights(app, miniapp):
    service = Insights(miniapp)
    for method, path, handler in [('GET', '/insights', service.report), ('GET', '/events', service.events),
                                  ('POST', '/events/scan', service.scan), ('POST', '/events/{id}/acknowledge', service.acknowledge)]:
        app.router.add_route(method, '/api/miniapp'+path, handler)
    return service


async def daily_control(session, now, tz_name):
    """Persist one owner digest per completed local day. No external sends here."""
    zone = ZoneInfo(tz_name)
    local = now.astimezone(zone)
    if local.hour < 1:
        return 0
    day = local.date()-timedelta(days=1)
    async def rows(conn, sql, **params):
        return list((await conn.execute(text(sql), params)).mappings())
    api = SimpleNamespace(rows=rows, tz=zone, today=lambda: local.date())
    owners = await rows(session, "SELECT u.id FROM users u JOIN user_roles r ON r.user_id=u.id WHERE u.active=TRUE AND r.role='OWNER'")
    count = 0
    for owner in owners:
        events = await sync_events(api, session, owner['id'])
        key = f'daily:{day}'
        if await rows(session, 'SELECT id FROM notifications WHERE user_id=:uid AND event_key=:key', uid=owner['id'], key=key):
            continue
        report = await compare_periods(api, session, day, day)
        m = report['metrics']
        summary = (f"Photo Boss · {day:%d.%m.%Y}\nВыручка {m['revenue']/100:,.2f} ₽ · поступило {m['cash']/100:,.2f} ₽\n"
                   f"Продаж {m['sales']} · съёмок {m['shootings']} · сотрудников {m['employees']}\n"
                   f"Опозданий {m['late']} · проблемных чеков {m['receiptIssues']} · открытых событий {len(events)}")
        await session.execute(text('''INSERT INTO notifications(user_id,text,sent,created_at,event_key,priority,kind,payload)
            VALUES (:uid,:title,FALSE,:now,:key,'info','daily_summary',:payload) ON CONFLICT(user_id,event_key) DO NOTHING'''),
            {'uid': owner['id'], 'title': summary, 'now': utc_now(), 'key': key, 'payload': json.dumps(report, ensure_ascii=False)})
        count += 1
    return count
