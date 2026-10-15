from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..config import config

SHIFT_START = time(9, 0)
LATE_FINE = 500.0
APPROVED_AI = ('ACCEPTED', 'MANUAL_APPROVED')


def shift_now(value=None):
    if value is None:
        value = datetime.now(UTC)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(config.training_timezone))


def is_late(value, scheduled_start=None):
    if scheduled_start is not None:
        if isinstance(scheduled_start, str):
            scheduled_start = datetime.fromisoformat(scheduled_start)
        if scheduled_start.tzinfo is None:
            scheduled_start = scheduled_start.replace(tzinfo=UTC)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC) > scheduled_start.astimezone(UTC)
    return shift_now(value).time().replace(tzinfo=None) > SHIFT_START


def _bounds(value=None):
    local = shift_now(value)
    lower = datetime.combine(local.date(), time.min, tzinfo=local.tzinfo).astimezone(UTC).replace(tzinfo=None)
    return local.date(), lower, lower + timedelta(days=1)


async def require_active_shift(session, user_id, hotel_id=None):
    """Require this employee's approved, still-open shift for the shoot hotel."""
    from ..models import Hotel, Shift, ShiftCheckIn, ShiftCheckOut
    day, lo, hi = _bounds()
    starts = (await session.scalars(select(ShiftCheckIn).where(
        ShiftCheckIn.user_id == user_id, ShiftCheckIn.shift_date == day,
        ShiftCheckIn.status == 'STARTED', ShiftCheckIn.ai_status.in_(APPROVED_AI),
    ))).all()
    plans = (await session.scalars(select(Shift).where(
        Shift.user_id == user_id, Shift.start_at >= lo, Shift.start_at < hi,
        Shift.status != 'CANCELLED',
    ))).all()
    plans_by_id = {plan.id: plan for plan in plans}
    hotel = await session.get(Hotel, hotel_id) if hotel_id is not None else None
    needs_hotel_scope = bool(hotel and any(getattr(hotel, key) is not None for key in ('latitude','longitude','attendance_radius_m')))
    for check in starts:
        plan = plans_by_id.get(check.scheduled_shift_id)
        if check.scheduled_shift_id is not None:
            if plan is None or (hotel_id is not None and plan.hotel_id != hotel_id):
                continue
        elif plans or needs_hotel_scope:
            continue
        scope = ShiftCheckOut.scheduled_shift_id == check.scheduled_shift_id if check.scheduled_shift_id is not None else ShiftCheckOut.scheduled_shift_id.is_(None)
        closed = await session.scalar(select(ShiftCheckOut.id).where(
            ShiftCheckOut.user_id == user_id, ShiftCheckOut.shift_date == day,
            scope, ShiftCheckOut.status == 'FINISHED',
        ))
        if not closed:
            return check
    raise ValueError('Сначала подтвердите начало смены с геолокацией и ИИ-проверкой фото для этого отеля. После завершения смены рабочие действия недоступны. Для проверки GPS-точки отеля администратор должен назначить смену в графике.')


async def require_active_shift_conn(api, conn, user_id, hotel_id=None):
    day, lo, hi = _bounds()
    rows = await api.rows(conn, '''SELECT i.* FROM shift_check_ins i
        LEFT JOIN shifts s ON s.id=i.scheduled_shift_id
        WHERE i.user_id=:uid AND i.shift_date=:day AND i.status='STARTED'
          AND i.ai_status IN ('ACCEPTED','MANUAL_APPROVED')
          AND ((i.scheduled_shift_id IS NOT NULL AND s.user_id=:uid AND s.status<>'CANCELLED'
                AND (CAST(:hid AS INTEGER) IS NULL OR s.hotel_id=CAST(:hid AS INTEGER)))
               OR (i.scheduled_shift_id IS NULL AND NOT EXISTS
                   (SELECT 1 FROM shifts p WHERE p.user_id=:uid AND p.start_at>=:lo AND p.start_at<:hi AND p.status<>'CANCELLED')
                   AND NOT EXISTS (SELECT 1 FROM hotels h WHERE h.id=CAST(:hid AS INTEGER)
                       AND (h.latitude IS NOT NULL OR h.longitude IS NOT NULL OR h.attendance_radius_m IS NOT NULL))))
          AND NOT EXISTS (SELECT 1 FROM shift_check_outs o WHERE o.user_id=i.user_id
              AND o.shift_date=i.shift_date AND o.status='FINISHED'
              AND (o.scheduled_shift_id=i.scheduled_shift_id OR (o.scheduled_shift_id IS NULL AND i.scheduled_shift_id IS NULL)))
        ORDER BY i.started_at DESC''', uid=user_id, day=day, lo=lo, hi=hi, hid=hotel_id)
    if not rows:
        raise ValueError('Сначала подтвердите начало смены с геолокацией и ИИ-проверкой фото для этого отеля. После завершения смены рабочие действия недоступны. Для проверки GPS-точки отеля администратор должен назначить смену в графике.')
    return rows[0]
