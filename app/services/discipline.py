"""Read-only discipline from the original schedule and attendance records."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..miniapp_security import utc_bounds
from ..models import utc_now
from .insights import change


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def period(api, conn, start, end):
    lo, hi = utc_bounds(start, end, api.tz)
    people = await api.rows(conn, 'SELECT id,name,active FROM users ORDER BY name,id')
    ins = await api.rows(conn, 'SELECT id,user_id,shift_date,status,late,started_at FROM shift_check_ins WHERE shift_date>=:start AND shift_date<=:end', start=start, end=end)
    outs = await api.rows(conn, 'SELECT id,user_id,shift_date,status,ended_at FROM shift_check_outs WHERE shift_date>=:start AND shift_date<=:end', start=start, end=end)
    plans = await api.rows(conn, "SELECT id,user_id,start_at,end_at FROM shifts WHERE start_at>=:lo AND start_at<:hi AND status<>'CANCELLED'", lo=lo, hi=hi)
    def grouped(rows):
        result = defaultdict(list)
        for row in rows:
            result[row['user_id']].append(row)
        return result
    starts_by, ends_by, plans_by = grouped(ins), grouped(outs), grouped(plans)
    result = {}
    now = utc(utc_now())
    for user in people:
        starts = {str(i['shift_date']): i for i in starts_by[user['id']]}
        ends = {str(o['shift_date']): o for o in ends_by[user['id']]}
        confirmed = {d for d, row in starts.items() if row['status'] == 'STARTED'}
        pending = {d for d, row in starts.items() if row['status'] == 'PENDING_REVIEW'}
        closed = {d for d, row in ends.items() if row['status'] == 'FINISHED'}
        pending_end = {d for d, row in ends.items() if row['status'] == 'PENDING_REVIEW'}
        late = {d for d, row in starts.items() if row['status'] == 'STARTED' and row['late']}
        unclosed = {d for d in confirmed-closed-pending_end if d < str(api.today())}
        missed = []
        daily_plans = {}
        for plan in plans_by[user['id']]:
            day = str(utc(plan['start_at']).astimezone(api.tz).date())
            daily_plans.setdefault(day, []).append(plan['id'])
            if utc(plan['end_at']) < now and day not in confirmed | pending | pending_end:
                missed.append({'shiftId': plan['id'], 'date': day})
        metrics = {'confirmed': len(confirmed), 'late': len(late), 'missed': len(missed),
                   'closed': len(closed), 'unclosed': len(unclosed), 'pending': len(pending | pending_end)}
        repeated = []
        if len(late) >= 3:
            repeated.append({'metric': 'late', 'count': len(late), 'threshold': 3, 'dates': sorted(late)})
        if len(missed) >= 2:
            repeated.append({'metric': 'missed', 'count': len(missed), 'threshold': 2, 'shifts': missed})
        days = sorted(set(starts) | set(ends) | set(daily_plans))
        result[user['id']] = dict(user) | metrics | {'repeated': repeated, 'missedShifts': missed,
            'trend': [{'date': d, 'confirmed': d in confirmed, 'late': d in late, 'closed': d in closed,
                       'pending': d in pending | pending_end, 'unclosed': d in unclosed,
                       'scheduledShiftIds': daily_plans.get(d, [])} for d in days]}
    return result


async def compare(api, conn, start, end):
    days = timedelta(days=(end-start).days+1)
    current = await period(api, conn, start, end)
    previous = await period(api, conn, start-days, end-days)
    keys = ('confirmed', 'late', 'missed', 'closed', 'unclosed', 'pending')
    rows = [row | {'changes': {key: change(row[key], previous[uid][key]) for key in keys}}
            for uid, row in current.items() if row['active'] or row['trend'] or previous[uid]['trend']]
    return {'from': str(start), 'to': str(end), 'previousFrom': str(start-days), 'previousTo': str(end-days),
            'items': rows, 'note': 'Пропуск — завершившаяся назначенная смена без подтверждённого выхода. Ожидающие проверки офлайн-отметки не считаются пропуском. Повторные нарушения показаны за выбранный период; дополнительных штрафов этот отчёт не создаёт.'}
