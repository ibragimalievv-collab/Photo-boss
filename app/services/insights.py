"""Explainable read-only checks. Money is calculated on the server in kopecks."""
import json
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text

from ..miniapp_security import utc_bounds
from .receipts import warnings_for


def cents(value):
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def change(current, previous):
    return {'current': current, 'previous': previous, 'absolute': current - previous,
            'percent': round((current - previous) / abs(previous) * 100, 1) if previous else None}


def parsed(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def receipt_check(row):
    data = parsed(row['analysis'])
    fields = data.get('fields') or {}
    created = row['created_at']
    created = datetime.fromisoformat(created) if isinstance(created, str) else created
    findings = warnings_for(fields, row['expected_amount'], created.date()) if data.get('status') == 'extracted' else ['Распознавание не завершено; нужна ручная проверка.']
    findings += ['Замечание модели: ' + str(v) for v in fields.get('concerns', [])]
    return {'level': 'owner_confirmed' if row['status'] == 'APPROVED' else 'image_extracted' if data.get('status') == 'extracted' else 'manual_required',
            'findings': findings, 'fields': fields,
            'limitation': 'Анализ изображения не подтверждает подлинность и поступление денег.'}


async def period_data(api, conn, start, end):
    lo, hi = utc_bounds(start, end, api.tz)
    params = {'lo': lo, 'hi': hi, 'start': start, 'end': end}
    sales = await api.rows(conn, '''SELECT s.*,b.hotel_id,b.manager_id,u.name AS employee
        FROM sales s LEFT JOIN bookings b ON b.id=s.booking_id
        JOIN users u ON u.id=s.credited_user_id WHERE s.created_at>=:lo AND s.created_at<:hi''', **params)
    receipts = await api.rows(conn, '''SELECT r.*,b.hotel_id FROM receipts r JOIN bookings b ON b.id=r.booking_id
        WHERE r.status='APPROVED' AND r.reviewed_at>=:lo AND r.reviewed_at<:hi''', **params)
    payroll = await api.rows(conn, '''SELECT p.*,u.name AS employee FROM payroll_entries p JOIN users u ON u.id=p.user_id
        WHERE p.created_at>=:lo AND p.created_at<:hi''', **params)
    bookings = await api.rows(conn, 'SELECT * FROM bookings WHERE shoot_date>=:start AND shoot_date<=:end', **params)
    shootings = await api.rows(conn, '''SELECT sh.id FROM shootings sh
        WHERE completed_at>=:lo AND completed_at<:hi''', **params)
    attendance = await api.rows(conn, '''SELECT * FROM shift_check_ins
        WHERE shift_date>=:start AND shift_date<=:end AND status='STARTED' ''', **params)
    bad_receipts = await api.rows(conn, '''SELECT * FROM receipts WHERE created_at>=:lo AND created_at<:hi''', **params)
    revenue = sum(cents(s['amount']) for s in sales)
    cash = sum(cents(r['verified_amount']) for r in receipts)
    accrued = sum(cents(s['commission']) for s in sales) + sum(cents(p['amount']) for p in payroll)
    employees, hotels = {}, {}
    names = {h['id']: h['name'] for h in await api.rows(conn, 'SELECT id,name FROM hotels')}
    user_names = {u['id']: u['name'] for u in await api.rows(conn, 'SELECT id,name FROM users')}
    for s in sales:
        e = employees.setdefault(s['credited_user_id'], {'id': s['credited_user_id'], 'name': s['employee'], 'revenue': 0, 'sales': 0, 'accrued': 0, 'bookings': 0})
        e['revenue'] += cents(s['amount']); e['sales'] += 1; e['accrued'] += cents(s['commission'])
        h = hotels.setdefault(s['hotel_id'], {'id': s['hotel_id'], 'name': names.get(s['hotel_id'], 'Без отеля'), 'revenue': 0, 'sales': 0, 'cash': 0})
        h['revenue'] += cents(s['amount']); h['sales'] += 1
        if s['manager_id'] is not None:
            m = employees.setdefault(s['manager_id'], {'id': s['manager_id'], 'name': user_names.get(s['manager_id'], ''), 'revenue': 0, 'sales': 0, 'accrued': 0, 'bookings': 0})
            m['bookedRevenue'] = m.get('bookedRevenue', 0) + cents(s['amount'])
            m['bookedSales'] = m.get('bookedSales', 0) + 1
    for r in receipts:
        h = hotels.setdefault(r['hotel_id'], {'id': r['hotel_id'], 'name': names.get(r['hotel_id'], 'Без отеля'), 'revenue': 0, 'sales': 0, 'cash': 0})
        h['cash'] += cents(r['verified_amount'])
    for p in payroll:
        e = employees.setdefault(p['user_id'], {'id': p['user_id'], 'name': p['employee'], 'revenue': 0, 'sales': 0, 'accrued': 0, 'bookings': 0})
        e['accrued'] += cents(p['amount'])
    user_names = {u['id']: u['name'] for u in await api.rows(conn, 'SELECT id,name FROM users')}
    for b in bookings:
        e = employees.setdefault(b['manager_id'], {'id': b['manager_id'], 'name': user_names.get(b['manager_id'], ''), 'revenue': 0, 'sales': 0, 'accrued': 0, 'bookings': 0})
        e['bookings'] += 1
    feedback = await api.rows(conn, 'SELECT rating FROM guest_feedback WHERE submitted_at>=:lo AND submitted_at<:hi AND rating IS NOT NULL', **params)
    metrics = {'guestReviews': len(feedback), 'guestRating': round(sum(r['rating'] for r in feedback)/len(feedback), 2) if feedback else 0, 'revenue': revenue, 'cash': cash, 'accrued': accrued, 'cashAfterAccruals': cash-accrued,
               'average': int((Decimal(revenue) / len(sales)).quantize(Decimal(1), rounding=ROUND_HALF_UP)) if sales else 0,
               'sales': len(sales), 'shootings': len(shootings), 'bookings': len(bookings),
               'cancellations': sum(b['status'] in ('CANCELLED', 'REJECTED') for b in bookings),
               'late': sum(bool(a['late']) for a in attendance), 'attendance': len(attendance),
               'employees': len({a['user_id'] for a in attendance}),
               'receiptIssues': sum(bool(receipt_check(r)['findings']) or r['status']=='REJECTED' for r in bad_receipts)}
    return {'from': str(start), 'to': str(end), 'metrics': metrics,
            'employees': sorted(employees.values(), key=lambda x: -x['revenue']), 'hotels': list(hotels.values()),
            'note': 'Остаток после начислений = подтверждённые поступления минус комиссии, премии и удержания. Расходы отелей, налоги и фактические выплаты не учтены: это не чистая прибыль.'}


async def compare_periods(api, conn, start, end, *, offset_days=None):
    duration = (end-start).days+1
    previous_start = start-timedelta(days=offset_days or duration)
    previous_end = end-timedelta(days=offset_days or duration)
    current = await period_data(api, conn, start, end)
    previous = await period_data(api, conn, previous_start, previous_end)
    changes = {k: change(v, previous['metrics'][k]) for k, v in current['metrics'].items()}
    flags = []
    c, p = current['metrics'], previous['metrics']
    if p['sales'] >= 5 and changes['revenue']['percent'] is not None and changes['revenue']['percent'] <= -30:
        flags.append({'metric': 'revenue', 'reason': 'Выручка снизилась не менее чем на 30%; в базе сравнения не менее 5 продаж.', **changes['revenue']})
    for key, denominator in [('cancellations', 'bookings'), ('late', 'attendance')]:
        if min(c[denominator], p[denominator]) >= 5:
            rate, old_rate = c[key]/c[denominator], p[key]/p[denominator]
            if c[key] >= 3 and rate-old_rate >= .2:
                flags.append({'metric': key, 'reason': 'Доля выросла не менее чем на 20 процентных пунктов; в обоих периодах не менее 5 наблюдений.',
                              'rate': rate, 'previousRate': old_rate, **changes[key]})
    if c['receiptIssues'] >= 3 and c['receiptIssues'] >= max(1,p['receiptIssues'])*2:
        flags.append({'metric': 'receiptIssues', 'reason': 'Не менее 3 проблемных чеков и рост числа не менее чем вдвое.', **changes['receiptIssues']})
    if end >= api.today():
        flags = []  # A partial trading day is not evidence of a sales drop.
    return {**current, 'previous': previous, 'changes': changes, 'flags': flags,
            'basis': 'Предыдущий период такой же длины. При нулевой базе процент не рассчитывается. Если текущий день не завершён, тревожные флаги по сравнению не строятся.'}


async def anomalies(api, conn):
    """Read all unresolved financial obligations; never repair anything here."""
    findings = []
    def add(key, title, entity, eid, evidence, priority='warning'):
        findings.append({'key': key, 'title': title, 'entity': entity, 'entityId': eid, 'evidence': evidence, 'priority': priority})
    sales = await api.rows(conn, '''SELECT s.*,sh.full_upload_completed_at,
        (SELECT COUNT(*) FROM photos p WHERE p.shooting_id=sh.id) AS frames
        FROM sales s LEFT JOIN shootings sh ON sh.booking_id=s.booking_id''')
    for s in sales:
        if s['payment_status'] != 'PAID':
            add(f"sale:{s['id']}:payment", 'Продажа оплачена не полностью', 'sale', s['id'], {'amount': cents(s['amount']), 'status': s['payment_status']})
        if s['amount'] <= 0 or s['sold_photos'] <= 0:
            add(f"sale:{s['id']}:amount", 'Некорректная сумма или количество кадров', 'sale', s['id'], {'amount': cents(s['amount']), 'soldPhotos': s['sold_photos']}, 'critical')
        if s['commission_role'] == 'PHOTOGRAPHER' and s['full_upload_completed_at']:
            expected = 15 if s['frames'] >= 150 else 10
            if Decimal(str(s['percent'])) != expected or abs(cents(s['commission'])-cents(Decimal(str(s['amount']))*expected/100)) > 0:
                add(f"sale:{s['id']}:commission", 'Комиссия отличается от правила 150 кадров', 'sale', s['id'], {'frames': s['frames'], 'expectedPercent': expected, 'actualPercent': s['percent'], 'commission': cents(s['commission'])}, 'critical')
    for row in await api.rows(conn, "SELECT id,entity_id,details,created_at FROM audit_logs WHERE entity='sales' AND action='row.update' ORDER BY id DESC LIMIT 500"):
        data = parsed(row['details'])
        before, after = data.get('before') or {}, data.get('after') or {}
        changed = {k: {'before': before.get(k), 'after': after.get(k)} for k in ('amount','credited_user_id','booking_id','sold_photos') if before.get(k) != after.get(k)}
        if changed:
            add(f"sale-audit:{row['id']}", 'Изменены существенные данные продажи', 'sale', row['entity_id'], {'auditId': row['id'], 'at': str(row['created_at']), 'changes': changed}, 'critical')
    receipts = await api.rows(conn, 'SELECT * FROM receipts ORDER BY id')
    hashes, operations = {}, {}
    for r in receipts:
        check = receipt_check(r)
        if check['findings'] and r['status'] != 'REJECTED':
            add(f"receipt:{r['id']}:check", 'Чек требует проверки', 'receipt', r['id'], check)
        for field, seen in [('image_sha256', hashes), ('operation_key', operations)]:
            value = r[field]
            if value and value in seen:
                add(f"receipt:{r['id']}:duplicate:{field}", 'Возможный повтор чека', 'receipt', r['id'], {'matchedReceiptId': seen[value], 'matchBy': field}, 'critical')
            if value: seen[value] = r['id']
    for r in await api.rows(conn, "SELECT id,receipt_id,amount FROM bank_reconciliations WHERE status='MISMATCH'"):
        add(f"bank:{r['id']}", 'Сумма банковской сверки не совпадает', 'receipt', r['receipt_id'], {'bankAmount': cents(r['amount'])}, 'critical')
    for r in await api.rows(conn, "SELECT id,shooting_id,attempts FROM photo_storage WHERE status='FAILED'"):
        add(f"upload:{r['id']}", 'Не удалось сохранить фото на Яндекс.Диске', 'shooting', r['shooting_id'], {'attempts': r['attempts']})
    for row in await api.rows(conn, "SELECT id,user_id,shift_date FROM shift_check_ins WHERE late=TRUE AND shift_date=:today", today=api.today()):
        add(f"late:{row['id']}", 'Опоздание на смену', 'shift_check_in', row['id'], {'employeeId': row['user_id'], 'date': str(row['shift_date'])})
    yesterday = api.today()-timedelta(days=1)
    for r in await api.rows(conn, '''SELECT i.id,i.user_id,i.shift_date FROM shift_check_ins i
        WHERE i.status='STARTED' AND i.shift_date<=:yesterday AND NOT EXISTS
        (SELECT 1 FROM shift_check_outs o WHERE o.user_id=i.user_id AND o.shift_date=i.shift_date AND o.status='FINISHED')''', yesterday=yesterday):
        add(f"shift:{r['id']}:open", 'Смена не закрыта', 'shift_check_in', r['id'], {'employeeId': r['user_id'], 'date': str(r['shift_date'])})
    return findings


async def sync_events(api, conn, owner_id):
    from ..models import utc_now
    now = utc_now()
    items = await anomalies(api, conn)
    active_keys = {item['key'] for item in items}
    old = await api.rows(conn, "SELECT id,event_key FROM notifications WHERE user_id=:uid AND kind='control' AND resolved_at IS NULL", uid=owner_id)
    for row in old:
        if row['event_key'] not in active_keys:
            await conn.execute(text('UPDATE notifications SET resolved_at=:now WHERE id=:id'), {'now': now, 'id': row['id']})
    for item in items:
        await conn.execute(text('''INSERT INTO notifications(user_id,text,sent,created_at,event_key,priority,kind,payload)
            VALUES (:uid,:title,FALSE,:now,:key,:priority,'control',:payload)
            ON CONFLICT(user_id,event_key) DO UPDATE SET text=excluded.text,payload=excluded.payload,resolved_at=NULL'''),
            {'uid': owner_id, 'title': item['title'], 'now': now, 'key': item['key'], 'priority': item['priority'], 'payload': json.dumps(item, ensure_ascii=False)})
    return items
