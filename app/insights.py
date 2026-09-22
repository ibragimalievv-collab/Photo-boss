"""Owner control center over existing ledgers."""
import json
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import AccessError, financial_period, require_owner
from .models import utc_now
from .services.insights import anomalies, compare_periods, parsed, sync_events


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
            from .cash_control import CATEGORIES
            result['expenseOptions']={'categories':CATEGORIES,'hotels':[dict(r) for r in await self.api.rows(conn,'SELECT id,name FROM hotels ORDER BY name')],
                'employees':[dict(r) for r in await self.api.rows(conn,'SELECT id,name FROM users ORDER BY name')], 'today':str(self.api.today())}
        return web.json_response(result)

    async def events(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        async with self.api.engine.connect() as conn:
            rows = await self.api.rows(conn, '''SELECT * FROM notifications WHERE user_id=:uid
                AND kind IN ('control','daily_summary') ORDER BY id DESC LIMIT 200''', uid=actor['id'])
            settings = await self.api.rows(conn,'SELECT value FROM settings WHERE key=:key',key=f'notify:owner:{actor["id"]}')
            preferences = {'daily':False,'critical':False} | (parsed(settings[0]['value']) if settings else {})
        return web.json_response({'items': [{'id': r['id'], 'title': r['text'], 'priority': r['priority'],
            'kind': r['kind'], 'at': str(r['created_at']), 'acknowledged': bool(r['acknowledged_at']),
            'resolved': bool(r['resolved_at']), 'data': parsed(r['payload'])} for r in rows], 'preferences':preferences})

    async def preferences(self, request):
        actor=request['miniapp_actor'];require_owner(actor['roles'])
        body=await self.api.body(request)
        if set(body)!={'daily','critical'} or any(type(v) is not bool for v in body.values()):
            raise AccessError('Выберите уведомления.',400)
        from .people import People
        async with self.api.engine.begin() as conn:
            require_owner(await People(self.api).current_editor(conn,actor['id']))
            key=f'notify:owner:{actor["id"]}'
            old=await self.api.rows(conn,'SELECT value FROM settings WHERE key=:key',key=key)
            await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) ON CONFLICT(key) DO UPDATE SET value=excluded.value'),{'key':key,'value':json.dumps(body)})
            await self.api.audit_write(conn,actor,'notification_preferences_updated','setting',None,json.dumps({'before':parsed(old[0]['value']) if old else None,'after':body}))
        return web.json_response({'ok':True})

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
                                  ('PUT','/events/preferences',service.preferences),('POST', '/events/scan', service.scan), ('POST', '/events/{id}/acknowledge', service.acknowledge)]:
        app.router.add_route(method, '/api/miniapp'+path, handler)
    return service


async def daily_control(session, now, tz_name):
    """Persist one owner digest per completed local day. No external sends here."""
    zone = ZoneInfo(tz_name)
    local = now.astimezone(zone)
    day = local.date()-timedelta(days=1)
    async def rows(conn, sql, **params):
        return list((await conn.execute(text(sql), params)).mappings())
    api = SimpleNamespace(rows=rows, tz=zone, today=lambda: local.date())
    owners = await rows(session, "SELECT u.id FROM users u JOIN user_roles r ON r.user_id=u.id WHERE u.active=TRUE AND r.role='OWNER'")
    count = 0
    findings = await anomalies(api,session) if owners else []
    for owner in owners:
        events = await sync_events(api, session, owner['id'],items=findings)
        if local.hour < 1:
            continue
        key = f'daily:{day}'
        if await rows(session, 'SELECT id FROM notifications WHERE user_id=:uid AND event_key=:key', uid=owner['id'], key=key):
            continue
        report = await compare_periods(api, session, day, day)
        m = report['metrics']
        summary = (f"Photo Boss · {day:%d.%m.%Y}\nВыручка {m['revenue']/100:,.2f} ₽ · поступило {m['cash']/100:,.2f} ₽\n"
                   f"Оплачено расходов {m['paidOut']/100:,.2f} ₽ · чистая касса {m['netCash']/100:,.2f} ₽\n"
                   f"Продаж {m['sales']} · съёмок {m['shootings']} · сотрудников {m['employees']}\n"
                   f"Опозданий {m['late']} · пропусков {m['missed']} · незакрытых смен {m['unclosed']}\n"
                   f"Отметок на проверке {m['pendingAttendance']} · проблемных чеков {m['receiptIssues']} · открытых событий {len(events)}")
        attention = sorted(events,key=lambda e:e['priority']!='critical')[:3]
        if attention:
            summary += '\nТребуют внимания:\n'+'\n'.join(f"• {e['title']} · {e['entity']} №{e['entityId']}" for e in attention)
        await session.execute(text('''INSERT INTO notifications(user_id,text,sent,created_at,event_key,priority,kind,payload)
            VALUES (:uid,:title,FALSE,:now,:key,'info','daily_summary',:payload) ON CONFLICT(user_id,event_key) DO NOTHING'''),
            {'uid': owner['id'], 'title': summary, 'now': utc_now(), 'key': key, 'payload': json.dumps(report, ensure_ascii=False)})
        count += 1
    return count
