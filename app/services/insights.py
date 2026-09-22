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
    receipts = await api.rows(conn, '''SELECT r.*,b.hotel_id,b.photographer_id FROM receipts r JOIN bookings b ON b.id=r.booking_id
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
    paid = await api.rows(conn, "SELECT * FROM cash_movements WHERE paid_on>=:start AND paid_on<=:end ORDER BY paid_on DESC,id DESC", **params)
    paid_out = sum(cents(r['amount']) for r in paid if r['status']=='POSTED')
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
        if r['photographer_id'] is not None:
            uid = r['photographer_id']
            e = employees.setdefault(uid, {'id': uid, 'name': user_names.get(uid, ''), 'revenue': 0, 'sales': 0, 'accrued': 0, 'bookings': 0})
            e['cash'] = e.get('cash', 0) + cents(r['verified_amount'])
    for p in payroll:
        e = employees.setdefault(p['user_id'], {'id': p['user_id'], 'name': p['employee'], 'revenue': 0, 'sales': 0, 'accrued': 0, 'bookings': 0})
        e['accrued'] += cents(p['amount'])
    user_names = {u['id']: u['name'] for u in await api.rows(conn, 'SELECT id,name FROM users')}
    for b in bookings:
        e = employees.setdefault(b['manager_id'], {'id': b['manager_id'], 'name': user_names.get(b['manager_id'], ''), 'revenue': 0, 'sales': 0, 'accrued': 0, 'bookings': 0})
        e['bookings'] += 1
    feedback = await api.rows(conn, 'SELECT rating FROM guest_feedback WHERE submitted_at>=:lo AND submitted_at<:hi AND rating IS NOT NULL', **params)
    from .discipline import period
    discipline = await period(api,conn,start,end)
    metrics = {'guestReviews': len(feedback), 'guestRating': round(sum(r['rating'] for r in feedback)/len(feedback), 2) if feedback else 0, 'revenue': revenue, 'cash': cash, 'accrued': accrued, 'cashAfterAccruals': cash-accrued, 'paidOut':paid_out, 'netCash':cash-paid_out,
               'average': int((Decimal(revenue) / len(sales)).quantize(Decimal(1), rounding=ROUND_HALF_UP)) if sales else 0,
               'sales': len(sales), 'shootings': len(shootings), 'bookings': len(bookings),
               'cancellations': sum(b['status'] in ('CANCELLED', 'REJECTED') for b in bookings),
               'late': sum(bool(a['late']) for a in attendance), 'attendance': len(attendance),
               'employees': len({a['user_id'] for a in attendance}),
               'missed': sum(r['missed'] for r in discipline.values()),
               'unclosed': sum(r['unclosed'] for r in discipline.values()),
               'pendingAttendance': sum(r['pending'] for r in discipline.values()),
               'receipts':len(bad_receipts),
               'receiptIssues': sum(r['status']=='REJECTED' or (bool(receipt_check(r)['findings']) and (r['status']=='PENDING' or parsed(r['analysis']).get('status')=='extracted')) for r in bad_receipts)}
    return {'from': str(start), 'to': str(end), 'metrics': metrics,
            'employees': sorted(employees.values(), key=lambda x: -x['revenue']), 'hotels': list(hotels.values()),
            'paidMovements':[{'id':r['id'],'category':r['category'],'amount':cents(r['amount']),'paidOn':str(r['paid_on']),'hotelId':r['hotel_id'],'employeeId':r['employee_id'],'note':r['note'],'status':r['status'],'voidReason':r['void_reason']} for r in paid[:200]],
            'paidMovementCount':len(paid),
            'note': 'Чистая касса за период = поступления по дате подтверждения чека минус внесённые оплаченные расходы по дате оплаты. Начисления сотрудникам вычитаются только из отдельного показателя «остаток после начислений». Неучтённые расходы и начальный остаток неизвестны; это не прибыль и не банковский баланс.'}


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
    for key, denominator in [('cancellations', 'bookings'), ('late', 'attendance'), ('receiptIssues','receipts')]:
        if min(c.get(denominator,0), p.get(denominator,0)) >= 5:
            rate, old_rate = c[key]/c[denominator], p[key]/p[denominator]
            if c[key] >= 3 and rate-old_rate >= .2:
                flags.append({'metric': key, 'reason': 'Доля выросла не менее чем на 20 процентных пунктов; в обоих периодах не менее 5 наблюдений.',
                              'rate': rate, 'previousRate': old_rate, **changes[key]})
    if end >= api.today():
        flags = []  # A partial trading day is not evidence of a sales drop.
    return {**current, 'previous': previous, 'changes': changes, 'flags': flags,
            'basis': 'Предыдущий период такой же длины. При нулевой базе процент не рассчитывается. Если текущий день не завершён, тревожные флаги по сравнению не строятся.'}


async def anomalies(api, conn):
    """Read all unresolved financial obligations; never repair anything here."""
    findings = []
    def add(key, title, entity, eid, evidence, priority='warning'):
        findings.append({'key': key, 'title': title, 'entity': entity, 'entityId': eid, 'evidence': evidence, 'priority': priority})
    sales = await api.rows(conn, '''SELECT s.*,b.hotel_id,b.package_id,b.manager_id,pe.amount AS manager_amount,pe.user_id AS manager_user_id,sh.full_upload_completed_at,
        (SELECT COUNT(*) FROM photos p WHERE p.shooting_id=sh.id) AS frames
        FROM sales s LEFT JOIN bookings b ON b.id=s.booking_id LEFT JOIN payroll_entries pe ON pe.id=s.manager_payroll_entry_id LEFT JOIN shootings sh ON sh.booking_id=s.booking_id''')
    for s in sales:
        if s['payment_status'] != 'PAID':
            add(f"sale:{s['id']}:payment", 'Продажа оплачена не полностью', 'sale', s['id'], {'amount': cents(s['amount']), 'status': s['payment_status']})
        if s['amount'] <= 0 or s['sold_photos'] <= 0:
            add(f"sale:{s['id']}:amount", 'Некорректная сумма или количество кадров', 'sale', s['id'], {'amount': cents(s['amount']), 'soldPhotos': s['sold_photos']}, 'critical')
        if s['commission_role'] == 'PHOTOGRAPHER' and s['full_upload_completed_at']:
            expected = 15 if s['frames'] >= 150 else 10
            if Decimal(str(s['percent'])) != expected or abs(cents(s['commission'])-cents(Decimal(str(s['amount']))*expected/100)) > 0:
                add(f"sale:{s['id']}:commission", 'Комиссия отличается от правила 150 кадров', 'sale', s['id'], {'frames': s['frames'], 'expectedPercent': expected, 'actualPercent': s['percent'], 'commission': cents(s['commission'])}, 'critical')
        if s['manager_percent_applied'] is not None:
            from decimal import InvalidOperation
            try:
                rate=Decimal(s['manager_percent_applied'])
                valid=rate.is_finite() and 0<=rate<=100
                expected_manager=cents(Decimal(str(s['amount']))*rate/100) if valid else None
            except (InvalidOperation,ValueError):
                expected_manager=None
            if expected_manager is None or s['manager_amount'] is None or s['manager_user_id']!=s['manager_id'] or cents(s['manager_amount'])!=expected_manager:
                add(f"sale:{s['id']}:manager",'Начисление менеджеру отличается от ставки продажи','sale',s['id'],
                    {'appliedPercent':s['manager_percent_applied'],'expectedAmount':expected_manager,'actualAmount':cents(s['manager_amount']) if s['manager_amount'] is not None else None,'payrollEntryId':s['manager_payroll_entry_id']},'critical')
    # Compare unit prices only within the same hotel/package and preceding 90 days.
    # No inference for sparse history and no automatic correction.
    from collections import defaultdict, deque
    from statistics import median
    history=defaultdict(deque)
    for s in sorted(sales,key=lambda r:(str(r['created_at']),r['id'])):
        if s['amount']<=0 or s['sold_photos']<=0 or s['hotel_id'] is None or s['package_id'] is None: continue
        at=datetime.fromisoformat(s['created_at']) if isinstance(s['created_at'],str) else s['created_at']
        group=history[(s['hotel_id'],s['package_id'])]
        while group and group[0][0]<at-timedelta(days=90): group.popleft()
        unit=Decimal(str(s['amount']))/s['sold_photos']
        if len(group)>=10:
            baseline=median(x[1] for x in group)
            if unit>=baseline*3 or unit<=baseline/5:
                add(f"sale:{s['id']}:unusual-price",'Цена кадра существенно отличается от истории','sale',s['id'],
                    {'unitPrice':cents(unit),'medianUnitPrice':cents(baseline),'sampleSize':len(group),'from':str(group[0][0]),'to':str(group[-1][0]),'hotelId':s['hotel_id'],'packageId':s['package_id'],'rule':'Не менее 10 предшествующих продаж за 90 дней; цена ≥3 медиан или ≤1/5 медианы. Возможное объяснение — изменение тарифа; нужна проверка.'})
        group.append((at,unit))
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
        if check['findings'] and (r['status']=='PENDING' or r['status']=='APPROVED' and parsed(r['analysis']).get('status')=='extracted'):
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
    for row in await api.rows(conn, "SELECT id,user_id,shift_date FROM shift_check_ins WHERE late=TRUE AND status='STARTED' AND shift_date=:today", today=api.today()):
        add(f"late:{row['id']}", 'Опоздание на смену', 'shift_check_in', row['id'], {'employeeId': row['user_id'], 'date': str(row['shift_date'])})
    yesterday = api.today()-timedelta(days=1)
    for r in await api.rows(conn, '''SELECT i.id,i.user_id,i.shift_date FROM shift_check_ins i
        WHERE i.status='STARTED' AND i.shift_date<=:yesterday AND NOT EXISTS
        (SELECT 1 FROM shift_check_outs o WHERE o.user_id=i.user_id AND o.shift_date=i.shift_date AND o.status IN ('FINISHED','PENDING_REVIEW'))''', yesterday=yesterday):
        add(f"shift:{r['id']}:open", 'Смена не закрыта', 'shift_check_in', r['id'], {'employeeId': r['user_id'], 'date': str(r['shift_date'])})
    lower, upper = utc_bounds(api.today()-timedelta(days=7),api.today(),api.tz)
    for r in await api.rows(conn, "SELECT id,cancellation_reason,cancelled_at,shoot_date FROM bookings WHERE status IN ('CANCELLED','REJECTED') AND ((cancelled_at>=:lo AND cancelled_at<:hi) OR (cancelled_at IS NULL AND shoot_date>=:start AND shoot_date<=:end))",lo=lower,hi=upper,start=api.today()-timedelta(days=7),end=api.today()+timedelta(days=7)):
        add(f"booking:{r['id']}:cancelled:{r['cancelled_at']}",'Запись отменена','booking',r['id'],{'shootDate':str(r['shoot_date']),'cancelledAt':str(r['cancelled_at']),'reason':r['cancellation_reason']})
    for table in ('shift_check_ins','shift_check_outs'):
        for r in await api.rows(conn,f"SELECT id,user_id,shift_date,offline_claimed_at,location_received_at FROM {table} WHERE status='PENDING_REVIEW'"):
            add(f"attendance:{table}:{r['id']}",'Офлайн-отметка ожидает проверки',table,r['id'],{'employeeId':r['user_id'],'date':str(r['shift_date']),'claimedAt':str(r['offline_claimed_at']),'receivedAt':str(r['location_received_at'])})
    for r in await api.rows(conn,"SELECT id,shooting_id,last_error FROM shoot_development_reviews WHERE status='FAILED'"):
        add(f"shoot-review:{r['id']}:failed",'AI-разбор не завершён','shooting',r['shooting_id'],{'reviewId':r['id'],'reason':r['last_error']})
    for r in await api.rows(conn,'''SELECT sh.id,b.photographer_id,MIN(s.created_at) AS first_sale FROM shootings sh JOIN bookings b ON b.id=sh.booking_id
        JOIN sales s ON s.booking_id=b.id WHERE sh.full_upload_completed_at IS NULL GROUP BY sh.id,b.photographer_id HAVING MIN(s.created_at)<:cutoff''',cutoff=lower+timedelta(days=6)):
        add(f"shooting:{r['id']}:full-upload",'После продажи не завершена полная загрузка','shooting',r['id'],{'employeeId':r['photographer_id'],'firstSaleAt':str(r['first_sale']),'rule':'Прошёл как минимум один завершённый местный день.'})
    checks = await api.rows(conn,"SELECT key,value FROM settings WHERE key LIKE 'checklist:%'")
    checks = [(r['key'],parsed(r['value'])) for r in checks if parsed(r['value']).get('active',True) and parsed(r['value']).get('required',False)]
    if checks:
        roles = {}
        for r in await api.rows(conn,'SELECT user_id,role FROM user_roles'):
            roles.setdefault(r['user_id'],set()).add(r['role'])
        closures = await api.rows(conn,"SELECT id,user_id,shift_date FROM shift_check_outs WHERE status IN ('FINISHED','PENDING_REVIEW') AND shift_date>=:start",start=api.today()-timedelta(days=7))
        done = {(r['user_id'],str(r['shift_date']),r['item_key']) for r in await api.rows(conn,'SELECT user_id,shift_date,item_key FROM work_checklist_completions WHERE done=TRUE AND shift_date>=:start',start=api.today()-timedelta(days=7))}
        for closure in closures:
            for key,item in checks:
                if str(closure['shift_date']) >= item.get('effectiveFrom',str(api.today())) and roles.get(closure['user_id'],set()) & set(item.get('roles',[])) and (closure['user_id'],str(closure['shift_date']),key) not in done:
                    add(f"required:{closure['user_id']}:{closure['shift_date']}:{key}",'Не выполнен настроенный обязательный пункт','shift_check_out',closure['id'],{'employeeId':closure['user_id'],'date':str(closure['shift_date']),'itemKey':key,'title':item['title']})
    report = await compare_periods(api,conn,yesterday-timedelta(days=6),yesterday)
    for flag in report['flags']:
        add(f"trend:{report['from']}:{report['to']}:{flag['metric']}",'Существенное изменение показателя','period',None,
            flag | {'from':report['from'],'to':report['to'],'previousFrom':report['previous']['from'],'previousTo':report['previous']['to']})
    return findings


async def sync_events(api, conn, owner_id, *, items=None):
    from ..models import utc_now
    now = utc_now()
    items = await anomalies(api, conn) if items is None else items
    active_keys = {item['key'] for item in items}
    old = await api.rows(conn, "SELECT id,event_key FROM notifications WHERE user_id=:uid AND kind='control' AND resolved_at IS NULL", uid=owner_id)
    for row in old:
        if row['event_key'] not in active_keys:
            await conn.execute(text('UPDATE notifications SET resolved_at=:now WHERE id=:id'), {'now': now, 'id': row['id']})
    for item in items:
        await conn.execute(text('''INSERT INTO notifications(user_id,text,sent,created_at,event_key,priority,kind,payload)
            VALUES (:uid,:title,FALSE,:now,:key,:priority,'control',:payload)
            ON CONFLICT(user_id,event_key) DO UPDATE SET text=excluded.text,payload=excluded.payload,
            acknowledged_at=CASE WHEN notifications.resolved_at IS NOT NULL THEN NULL ELSE notifications.acknowledged_at END,
            sent=CASE WHEN notifications.resolved_at IS NOT NULL THEN FALSE ELSE notifications.sent END,
            priority=excluded.priority,resolved_at=NULL'''),
            {'uid': owner_id, 'title': item['title'], 'now': now, 'key': item['key'], 'priority': item['priority'], 'payload': json.dumps(item, ensure_ascii=False)})
    return items
