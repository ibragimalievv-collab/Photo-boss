"""Automatic time-based attendance charges with scoped, audited exceptions."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import text

from .hotel_points import context_key, read_json, review_required, revision, save_json
from .miniapp_security import AccessError, utc_bounds

logger = logging.getLogger(__name__)
RULES_KEY = 'attendance:fines:rules'
SINCE_KEY = 'attendance:fines:activated_at'
DEFAULT_RULES = {'lateAmount': 500, 'earlyAmount': None, 'missingEndAmount': None}
LABELS = {'late': 'Опоздание / нет прихода вовремя', 'early': 'Ранний уход', 'missing_end': 'Нет отметки ухода'}
RULE_FIELD = {'late': 'lateAmount', 'early': 'earlyAmount', 'missing_end': 'missingEndAmount'}


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def fine_key(uid, day, kind):
    return f'attendance:fine:{day}:{uid}:{kind}'


def amount(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) not in (int, float) or not 0 <= value <= 1000000:
        raise AccessError('Сумма должна быть от 0 до 1 000 000 ₽.', 400)
    result = Decimal(str(value))
    if not result.is_finite() or result != result.quantize(Decimal('.01')):
        raise AccessError('Укажите сумму с точностью до копейки.', 400)
    return float(result)


async def initialize(api, now):
    async with api.engine.begin() as conn:
        await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) ON CONFLICT (key) DO NOTHING'),
                           {'key': SINCE_KEY, 'value': datetime.combine(now.astimezone(api.tz).date(), time.min, api.tz).astimezone(timezone.utc).isoformat()})
    logger.info('Attendance fines enabled: late=existing 500 RUB; departure rates require explicit owner configuration')


async def rules(api, conn):
    return await read_json(api, conn, RULES_KEY) or dict(DEFAULT_RULES)


async def record_charge(api, conn, uid, day, kind, detected, when, expected, now, *, rule):
    key = fine_key(uid, day, kind)
    old = await read_json(api, conn, key)
    if not detected and not old:
        return
    rule = old.get('ruleAmount', rule) if old else rule
    calculated = rule if detected and rule is not None else 0
    value = {'userId': uid, 'date': str(day), 'kind': kind, 'label': LABELS[kind],
             'detected': detected, 'ruleAmount': rule, 'ruleConfigured': rule is not None, 'calculatedAmount': calculated,
             'expectedAt': expected.isoformat(), 'observedAt': when.isoformat() if when else None}
    override = old.get('override') if old else None
    effective = override['amount'] if override else calculated
    value.update(amount=effective, override=override, payrollId=old.get('payrollId') if old else None)
    if old is None and kind == 'late':
        starts = await api.rows(conn, 'SELECT id FROM shift_check_ins WHERE user_id=:uid AND shift_date=:day', uid=uid, day=day)
        if starts:
            marker = f'shift_check_in={starts[0]["id"]}'
            legacy = await api.rows(conn, '''SELECT id FROM payroll_entries WHERE user_id=:uid AND period=:day
                AND kind='Штраф за опоздание' AND (note LIKE :middle OR note LIKE :ending) ORDER BY id LIMIT 1''',
                uid=uid, day=str(day), middle=f'%{marker};%', ending=f'%{marker}')
            if legacy:
                value['payrollId'] = legacy[0]['id']
    if old and all(old.get(k) == v for k, v in value.items()):
        return
    if value['payrollId']:
        await conn.execute(text('UPDATE payroll_entries SET amount=:amount WHERE id=:id AND user_id=:uid'),
                           {'amount': -effective, 'id': value['payrollId'], 'uid': uid})
    elif effective:
        row = (await api.rows(conn, '''INSERT INTO payroll_entries(user_id,kind,amount,period,note,created_at)
            VALUES (:uid,:kind,:amount,:period,:note,:now) RETURNING id''', uid=uid,
            kind='Штраф: '+LABELS[kind], amount=-effective, period=str(day), note=key,
            now=now.replace(tzinfo=None)))[0]
        value['payrollId'] = row['id']
    value['updatedAt'] = now.isoformat()
    await save_json(conn, key, value)
    await api.audit_write(conn, {'id': uid}, 'attendance_fine_calculated', 'attendance', uid,
                          json.dumps({'date': str(day), 'kind': kind, 'before': old, 'after': value}, ensure_ascii=False))


async def reconcile_day(api, conn, uid, day, now):
    """Caller holds the user's row lock; every day/kind owns one ledger entry."""
    since = await api.rows(conn, 'SELECT value FROM settings WHERE key=:key', key=SINCE_KEY)
    if since and day < utc(since[0]['value']).astimezone(api.tz).date():
        return
    policy = await rules(api, conn)
    lo, hi = utc_bounds(day, day, api.tz)
    plans = await api.rows(conn, '''SELECT start_at,end_at FROM shifts WHERE user_id=:uid
        AND status<>'CANCELLED' AND start_at>=:lo AND start_at<:hi ORDER BY start_at''', uid=uid, lo=lo, hi=hi)
    expected_start = min(utc(p['start_at']) for p in plans) if plans else datetime.combine(day, time(9), api.tz).astimezone(timezone.utc)
    expected_end = max(utc(p['end_at']) for p in plans) if plans else None
    starts = await api.rows(conn, 'SELECT * FROM shift_check_ins WHERE user_id=:uid AND shift_date=:day', uid=uid, day=day)
    ends = await api.rows(conn, 'SELECT * FROM shift_check_outs WHERE user_id=:uid AND shift_date=:day', uid=uid, day=day)

    async def observed(rows, purpose, at):
        if not rows or rows[0]['status'] == 'REJECTED':
            return None
        row = rows[0]
        if row[at]:
            return utc(row[at])
        if row['status'] != 'PENDING_REVIEW':
            return None
        context = await read_json(api, conn, context_key(purpose, row['id']))
        # Offline device time cannot silently erase a timer-generated charge.
        # A manager may verify the real time or enter a justified exception.
        field = 'location_received_at' if context and context.get('source') == 'offline' else 'offline_claimed_at'
        return utc(row[field]) if row[field] else None

    arrived = await observed(starts, 'start', 'started_at')
    left = await observed(ends, 'end', 'ended_at')
    late = arrived > expected_start if arrived else bool(plans and now > expected_start)
    await record_charge(api, conn, uid, day, 'late', late, arrived, expected_start, now, rule=policy['lateAmount'])
    if expected_end:
        await record_charge(api, conn, uid, day, 'early', bool(left and left < expected_end), left, expected_end, now, rule=policy['earlyAmount'])
        await record_charge(api, conn, uid, day, 'missing_end', bool(not left and now > expected_end), left, expected_end, now, rule=policy['missingEndAmount'])
    if starts:
        charge = await read_json(api, conn, fine_key(uid, day, 'late'))
        await conn.execute(text('UPDATE shift_check_ins SET late=:late,fine_amount=:amount WHERE id=:id'),
                           {'late': late, 'amount': charge['amount'] if charge else 0, 'id': starts[0]['id']})


async def sweep(api, now):
    async with api.engine.begin() as conn:
        since_rows = await api.rows(conn, 'SELECT value FROM settings WHERE key=:key', key=SINCE_KEY)
        if not since_rows:
            return
        since = utc(since_rows[0]['value'])
        # The feature starts with new scheduled shifts. It cannot recreate the
        # fines reset earlier today or penalize retained historical plans.
        plans = await api.rows(conn, '''SELECT DISTINCT s.user_id,s.start_at FROM shifts s JOIN users u ON u.id=s.user_id
            WHERE s.status<>'CANCELLED' AND s.start_at>=:since AND s.start_at<:now AND u.active=TRUE
            AND EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_id=u.id AND ur.role IN ('PHOTOGRAPHER','MANAGER'))''',
            since=max(since, now-timedelta(days=7)).replace(tzinfo=None), now=now.replace(tzinfo=None))
        days = sorted({(r['user_id'], utc(r['start_at']).astimezone(api.tz).date()) for r in plans})
        for uid, day in days:
            await api.rows(conn, 'SELECT id FROM users WHERE id=:uid FOR UPDATE', uid=uid)
            await reconcile_day(api, conn, uid, day, now)


async def fine_loop(api):
    from .operations_guard import operation_lease
    while True:
        try:
            async with operation_lease(api.engine):
                await sweep(api, datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Attendance fine calculation failed; will retry')
        await asyncio.sleep(30)


class AttendanceFines:
    def __init__(self, api):
        self.api = api

    async def listing(self, request):
        from .attendance import Attendance
        from .people import People, check_editor
        actor = request['miniapp_actor']
        check_editor(actor['roles'])
        async with self.api.engine.begin() as conn:
            current = await People(self.api).current_editor(conn, actor['id'])
            actor = actor | {'roles': current}
            entries = await self.api.rows(conn, "SELECT key,value FROM settings WHERE key LIKE 'attendance:fine:%' ORDER BY key DESC")
            items = []
            for entry in entries:
                value = json.loads(entry['value'])
                day = datetime.fromisoformat(value['date']).date()
                if not await Attendance(self.api).day_in_scope(conn, actor, value['userId'], day):
                    continue
                user = await self.api.rows(conn, 'SELECT name FROM users WHERE id=:uid', uid=value['userId'])
                items.append(value | {'name': user[0]['name'] if user else 'Сотрудник', 'revision': revision(value)})
                if len(items) >= 200:
                    break
            policy = await rules(self.api, conn)
        return web.json_response({'items': sorted(items, key=lambda r:(r['date'],r['userId'],r['kind']), reverse=True),
            'rules': policy, 'rulesRevision': revision(policy), 'canEditRules': 'OWNER' in current,
            'enabled': review_required()}, headers={'Cache-Control':'no-store'})

    async def edit(self, request):
        from .attendance import Attendance
        from .people import People, positive_id
        body, actor = await self.api.body(request), request['miniapp_actor']
        if set(body) != {'userId','date','kind','amount','reason','revision'} or not isinstance(body['kind'],str) or body['kind'] not in LABELS:
            raise AccessError('Обновите форму штрафа.', 400)
        uid, day = positive_id(body['userId']), self.api.day(body['date'])
        if uid == actor['id']:
            raise AccessError('Свой штраф может исправить другой руководитель.', 403)
        effective = amount(body['amount'])
        reason = body['reason']
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 1000:
            raise AccessError('Укажите причину изменения.', 400)
        async with self.api.engine.begin() as conn:
            current = await People(self.api).current_editor(conn, actor['id'], uid)
            await Attendance(self.api).require_day_scope(conn, actor | {'roles':current}, uid, day)
            key = fine_key(uid, day, body['kind'])
            old = await read_json(self.api, conn, key)
            if not old:
                raise AccessError('Начисление не найдено.', 404)
            if revision(old) != body['revision']:
                raise AccessError('Начисление уже изменено. Обновите список.', 409)
            now = datetime.now(timezone.utc)
            new = old | {'amount':effective, 'override':{'amount':effective,'reason':reason.strip(),'actorId':actor['id'],'at':now.isoformat()}}
            if old.get('payrollId'):
                await conn.execute(text('UPDATE payroll_entries SET amount=:amount WHERE id=:id AND user_id=:uid'),
                    {'amount':-effective,'id':old['payrollId'],'uid':uid})
            elif effective:
                payroll = (await self.api.rows(conn, '''INSERT INTO payroll_entries(user_id,kind,amount,period,note,created_at)
                    VALUES (:uid,:kind,:amount,:period,:note,:now) RETURNING id''', uid=uid, kind='Штраф: '+LABELS[body['kind']],
                    amount=-effective,period=str(day),note=key,now=now.replace(tzinfo=None)))[0]
                new['payrollId'] = payroll['id']
            await save_json(conn, key, new)
            if body['kind'] == 'late':
                await conn.execute(text('UPDATE shift_check_ins SET fine_amount=:amount WHERE user_id=:uid AND shift_date=:day'),
                    {'amount':effective,'uid':uid,'day':day})
            await self.api.audit_write(conn, actor, 'attendance_fine_adjusted', 'attendance', uid,
                json.dumps({'before':old,'after':new},ensure_ascii=False))
        return web.json_response({'ok':True})

    async def save_rules(self, request):
        from .people import People
        body, actor = await self.api.body(request), request['miniapp_actor']
        if set(body) != {'rules','revision'} or not isinstance(body['rules'],dict) or set(body['rules']) != set(DEFAULT_RULES):
            raise AccessError('Проверьте правила штрафов.',400)
        values = {key:amount(value,optional=key!='lateAmount') for key,value in body['rules'].items()}
        async with self.api.engine.begin() as conn:
            current = await People(self.api).current_editor(conn,actor['id'])
            if 'OWNER' not in current:
                raise AccessError('Правила штрафов изменяет владелец.',403)
            previous = await rules(self.api,conn)
            if body['revision'] != revision(previous):
                raise AccessError('Правила уже изменены. Обновите список.',409)
            await save_json(conn,RULES_KEY,values)
            await self.api.audit_write(conn,actor,'attendance_fine_rules_saved','settings',0,
                json.dumps({'before':previous,'after':values},ensure_ascii=False))
        return web.json_response({'ok':True})


def install_attendance_fines(app, api):
    service = AttendanceFines(api)
    app.router.add_get('/api/miniapp/attendance/fines', service.listing)
    app.router.add_put('/api/miniapp/attendance/fines', service.edit)
    app.router.add_put('/api/miniapp/attendance/fine-rules', service.save_rules)


def worker_api(engine, tz_name):
    async def rows(conn, sql, **params):
        return list((await conn.execute(text(sql),params)).mappings())
    async def audit_write(conn, actor, action, entity, entity_id, details):
        await conn.execute(text('''INSERT INTO audit_logs(user_id,action,entity,entity_id,details,created_at)
            VALUES (:uid,:action,:entity,:eid,:details,:now)'''),
            {'uid':actor['id'],'action':action,'entity':entity,'eid':entity_id,'details':details,
             'now':datetime.now(timezone.utc).replace(tzinfo=None)})
    return SimpleNamespace(engine=engine,tz=ZoneInfo(tz_name),rows=rows,audit_write=audit_write)
