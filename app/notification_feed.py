"""Current notification visibility as a SQL predicate, shared by paging/count/read.

Delivery keeps its existing per-event checks. The feed must count the whole
eligible unread set in SQL without fetching history into the application.
"""
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    String,
    and_,
    case,
    cast,
    false,
    func,
    literal,
    not_,
    or_,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB

from .config import config
from .models import (
    Booking,
    HotelEmployee,
    Notification,
    Sale,
    SaleDiscountApproval,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    WorkBreak,
)


def details(value):
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


class Json:
    def __init__(self, column, postgres):
        self.postgres = postgres
        if postgres:
            self.value = case((func.pg_input_is_valid(func.coalesce(column, '{}'), 'jsonb'),
                               cast(column, JSONB)), else_=cast(literal('{}'), JSONB))
        else:
            self.value = case((func.json_valid(column), column), else_=literal('{}'))

    def type(self, key):
        return func.jsonb_typeof(func.jsonb_extract_path(self.value, key)) if self.postgres else func.json_type(self.value, '$.'+key)

    def text(self, key):
        return func.jsonb_extract_path_text(self.value, key) if self.postgres else cast(func.json_extract(self.value, '$.'+key), String)

    def integer(self, key):
        text = self.text(key)
        if self.postgres:
            valid = and_(self.type(key) == 'number', text.op('~')('^[0-9]{1,18}$'))
        else:
            valid = self.type(key) == 'integer'
        return case((valid, cast(text, BigInteger)))

    def hotels(self):
        if self.postgres:
            value = case((self.type('hotelIds') == 'array', func.jsonb_extract_path(self.value, 'hotelIds', type_=JSONB)), else_=cast(literal('[]'), JSONB))
            rows = func.jsonb_array_elements(value).table_valued('value').alias('notice_hotels')
            raw = cast(rows.c.value, String)
            valid = and_(func.jsonb_typeof(rows.c.value) == 'number', raw.op('~')('^[0-9]{1,18}$'))
            number = case((valid, cast(raw, BigInteger)))
            length = func.jsonb_array_length(value)
            first_text = func.jsonb_extract_path_text(value, '0')
            first = case((and_(func.jsonb_typeof(func.jsonb_extract_path(value, '0')) == 'number',
                first_text.op('~')('^[0-9]{1,18}$')), cast(first_text, BigInteger)))
        else:
            rows = func.json_each(self.value, '$.hotelIds').table_valued('value', 'type').alias('notice_hotels')
            number = case((rows.c.type == 'integer', cast(rows.c.value, BigInteger)))
            length = func.json_array_length(self.value, '$.hotelIds')
            first = case((func.json_type(self.value, '$.hotelIds[0]') == 'integer',
                          cast(func.json_extract(self.value, '$.hotelIds[0]'), BigInteger)))
        return rows, number, length, first


async def sqlite_helpers(session):
    """Pure parsing/timezone functions on the local SQLite connection, no DDL."""
    from .services.booking_notices import notice_booking_id
    from .services.booking_reminders import scheduled_at
    connection = await session.connection()
    if connection.dialect.name != 'sqlite':
        return
    def booking_id(kind, payload, key, message):
        return notice_booking_id(SimpleNamespace(kind=kind, payload=payload, event_key=key, text=message))
    def scheduled(day, at):
        try:
            return scheduled_at(SimpleNamespace(shoot_date=date.fromisoformat(day), shoot_time=time.fromisoformat(at))).isoformat()
        except (TypeError, ValueError):
            return None
    def boundary(day, end):
        try:
            d = date.fromisoformat(day) + timedelta(days=int(end))
            return datetime.combine(d, time(), ZoneInfo(config.training_timezone)).astimezone(UTC).replace(tzinfo=None).isoformat(' ', timespec='microseconds')
        except (TypeError, ValueError):
            return None
    def sale_id(key):
        try:
            value = int(key.rsplit(':', 1)[1])
            return value if -(2**63) <= value < 2**63 else None
        except (AttributeError, IndexError, TypeError, ValueError):
            return None
    raw = await connection.get_raw_connection()
    await raw.driver_connection.create_function('pb_notice_booking_id', 4, booking_id, deterministic=True)
    await raw.driver_connection.create_function('pb_notice_scheduled_at', 2, scheduled, deterministic=True)
    await raw.driver_connection.create_function('pb_notice_day_boundary', 2, boundary, deterministic=True)
    await raw.driver_connection.create_function('pb_notice_sale_id', 1, sale_id, deterministic=True)


async def visibility(session, actor, roles):
    """SQL equivalent of eligible(...include_read=True, for_delivery=False)."""
    from .booking_control import preferences
    from .push_notifications import KINDS, current_utc
    from .services.assignment_notifications import ACTIVE
    from .services.booking_reminders import PRE_SHOOT
    await sqlite_helpers(session)
    pg = session.get_bind().dialect.name == 'postgresql'
    n = Notification
    data = Json(n.payload, pg)
    owner, admin = 'OWNER' in roles, 'ADMIN' in roles
    hotels = set(await session.scalars(select(HotelEmployee.hotel_id).where(
        HotelEmployee.user_id == actor.id))) if admin and not owner else set()
    def supervised(hotel):
        return true() if owner else hotel.in_(hotels) if admin else false()
    def booking_exists(*conditions):
        return select(Booking.id).where(Booking.id == bid, *conditions).correlate(n).exists()
    def not_trashed(booking_id):
        return ~select(Setting.key).where(Setting.key == literal('booking:trash:')+cast(booking_id, String)).correlate(n, Booking).exists()
    def id_string(text):
        return case((text.op('~')('^[0-9]{1,18}$'), cast(text, BigInteger)))
    if pg:
        key_bid = case((or_(*(n.event_key.startswith(p) for p in (
            'sale-ready:', 'booking-assigned:', 'shoot-reminder:', 'rescheduled:', 'cancelled:'))),
            id_string(func.split_part(n.event_key, ':', 2))))
        old_bid = case((n.kind == 'sale_ready', id_string(func.substring(n.text, r'(?i)Съ[её]мка\s*[№#]\s*(\d+)'))))
        payload_bid = data.integer('bookingId')
        bid = func.coalesce(case((payload_bid > 0, payload_bid)), key_bid, old_bid)
        moment = func.timezone('UTC', func.timezone(config.training_timezone, Booking.shoot_date+Booking.shoot_time))
        scheduled = (func.to_char(moment, 'YYYY-MM-DD"T"HH24:MI:SS') +
            case((func.to_char(moment, 'US') != '000000', literal('.')+func.to_char(moment, 'US')), else_='') + '+00:00')
    else:
        bid = func.pb_notice_booking_id(n.kind, n.payload, n.event_key, n.text)
        scheduled = func.pb_notice_scheduled_at(cast(Booking.shoot_date, String), cast(Booking.shoot_time, String))
    # Ordinary staff retain business events, while owner-only diagnostics remain private.
    branches = [n.kind.in_(KINDS - {'operational_alert', 'work_event', 'service_status',
        'manager_idle', 'manager_advice', 'booking_assignment', 'booking_reminder', 'sale_ready',
        'reminder', 'lunch_start', 'lunch_end', 'lunch_corrected', 'lunch_attention',
        'attendance', 'attendance_fine', 'sale_discount', 'sale_created', 'daily_summary', 'control'})]
    if owner:
        branches.append(n.kind.in_({'work_event', 'service_status'}))
    branches.append(and_(n.kind == 'operational_alert', select(Booking.id).where(
        Booking.id == data.integer('bookingId'), supervised(Booking.hotel_id)).correlate(n).exists()))
    assignment = and_(Booking.photographer_id == actor.id, literal('PHOTOGRAPHER' in roles), not_trashed(bid))
    branches.append(and_(n.kind == 'booking_assignment', booking_exists(assignment, Booking.status.in_(ACTIVE))))
    branches.append(and_(n.kind == 'booking_reminder', booking_exists(assignment,
        Booking.status.in_(PRE_SHOOT), data.text('scheduledAt') == scheduled)))
    related_booking = or_(supervised(Booking.hotel_id), Booking.manager_id == actor.id, Booking.photographer_id == actor.id)
    branches.append(and_(n.kind.in_({'sale_ready', 'reminder'}), bid.is_not(None),
        booking_exists(not_trashed(bid), related_booking)))
    branches.append(and_(n.kind == 'reminder', bid.is_(None)))
    branches.append(and_(n.kind.in_({'lunch_start', 'lunch_end', 'lunch_corrected', 'lunch_attention'}),
        select(WorkBreak.id).where(WorkBreak.id == data.integer('workBreakId'),
            or_(supervised(WorkBreak.hotel_id), WorkBreak.user_id == actor.id)).correlate(n).exists()))
    # Attendance supervisors require every hotel planned on that employee/day.
    def attendance_scope(uid, day):
        if owner:
            return true()
        if not admin:
            return uid == actor.id
        if pg:
            parsed = case((func.pg_input_is_valid(day, 'date'), cast(day, Date)))
            lower = func.timezone('UTC', func.timezone(config.training_timezone, cast(parsed, DateTime)))
            upper = func.timezone('UTC', func.timezone(config.training_timezone, cast(parsed+1, DateTime)))
        else:
            lower, upper = func.pb_notice_day_boundary(day, 0), func.pb_notice_day_boundary(day, 1)
        planned = (Shift.user_id == uid, Shift.status != 'CANCELLED', Shift.start_at < upper, Shift.end_at > lower)
        return or_(uid == actor.id, and_(select(Shift.id).where(*planned).correlate(n, ShiftCheckIn, ShiftCheckOut, Setting).exists(),
            ~select(Shift.id).where(*planned, Shift.hotel_id.not_in(hotels)).correlate(n, ShiftCheckIn, ShiftCheckOut, Setting).exists()))
    attendance = []
    for purpose, model in [('start', ShiftCheckIn), ('end', ShiftCheckOut)]:
        attendance.append(and_(data.text('purpose') == purpose, select(model.id).where(
            model.id == data.integer('attendanceId'), model.user_id == data.integer('userId'),
            cast(model.shift_date, String) == data.text('date'),
            attendance_scope(model.user_id, cast(model.shift_date, String))).correlate(n).exists()))
    branches.append(and_(n.kind == 'attendance', or_(*attendance)))
    charge = Json(Setting.value, pg)
    fine_key = literal('attendance:fine:')+data.text('date')+':'+cast(data.integer('userId'), String)+':'+data.text('fineKind')
    # A valid calendar date is part of the persisted fine identity.
    valid_day = (and_(data.text('date').op('~')(r'^\d{4}-\d{2}-\d{2}$'),
                     func.pg_input_is_valid(data.text('date'), 'date')) if pg else
                 func.date(data.text('date'), '+0 days') == data.text('date'))
    branches.append(and_(n.kind == 'attendance_fine', valid_day,
        data.text('fineKind').in_({'late', 'early', 'missing_end'}), data.text('fineKey') == fine_key,
        select(Setting.key).where(Setting.key == fine_key,
            charge.integer('userId') == data.integer('userId'), charge.text('date') == data.text('date'),
            charge.text('kind') == data.text('fineKind'),
            attendance_scope(data.integer('userId'), data.text('date'))).correlate(n).exists()))
    # Discount requests use the same current booking scope as the workflow.
    access = or_(supervised(Booking.hotel_id),
        and_(literal('MANAGER' in roles), Booking.manager_id == actor.id),
        and_(literal('PHOTOGRAPHER' in roles), Booking.photographer_id == actor.id))
    if admin and not owner:
        access = supervised(Booking.hotel_id)  # ADMIN is scoped before other roles.
    branches.append(and_(n.kind == 'sale_discount', select(SaleDiscountApproval.id).join(
        Booking, Booking.id == SaleDiscountApproval.booking_id).where(
            SaleDiscountApproval.id == data.integer('discountId'), Booking.id == data.integer('bookingId'),
            not_trashed(Booking.id), access,
            or_(literal(owner or admin), SaleDiscountApproval.requested_by_id == actor.id)).correlate(n).exists()))
    if pg:
        sale_id = id_string(func.regexp_replace(n.event_key, '^.*:', ''))
    else:
        sale_id = func.pb_notice_sale_id(n.event_key)
    branches.append(and_(n.kind == 'sale_created', select(Sale.id).join(Booking, Booking.id == Sale.booking_id).where(
        Sale.id == sale_id, or_(supervised(Booking.hotel_id), Booking.manager_id == actor.id,
                              Sale.credited_user_id == actor.id)).correlate(n).exists()))
    settings = await preferences(session)
    rows, hotel, length, first_hotel = data.hotels()
    valid_hotels = and_(data.type('hotelIds') == 'array', length > 0,
        ~select(literal(1)).select_from(rows).where(or_(hotel.is_(None), hotel <= 0)).correlate(n).exists())
    if settings['enabled']:
        permitted = true() if owner else and_(literal(admin),
            ~select(literal(1)).select_from(rows).where(hotel.not_in(hotels)).correlate(n).exists())
        branches.append(and_(n.kind == 'manager_advice', valid_hotels, permitted))
        day = current_utc().astimezone(ZoneInfo(config.training_timezone)).date()
        started = select(ShiftCheckIn.id).where(ShiftCheckIn.user_id == actor.id,
            ShiftCheckIn.shift_date == day, ShiftCheckIn.status == 'STARTED').exists()
        ended = select(ShiftCheckOut.id).where(ShiftCheckOut.user_id == actor.id,
            ShiftCheckOut.shift_date == day, ShiftCheckOut.status == 'FINISHED').exists()
        now = current_utc().replace(tzinfo=None)
        branches.append(and_(n.kind == 'manager_idle', literal('MANAGER' in roles), valid_hotels,
            length == 1, data.integer('managerId') == actor.id, started, not_(ended),
            select(Shift.id).where(Shift.id == data.integer('shiftId'), Shift.user_id == actor.id,
                Shift.hotel_id == first_hotel, Shift.status != 'CANCELLED',
                Shift.start_at <= now, Shift.end_at > now).correlate(n).exists()))
    preference = await session.get(Setting, f'notify:owner:{actor.id}') if owner else None
    pref = details(preference.value) if preference else {}
    if pref.get('daily'):
        branches.append(n.kind == 'daily_summary')
    if pref.get('critical'):
        branches.append(and_(n.kind == 'control', n.priority == 'critical'))
    return and_(n.user_id == actor.id, n.resolved_at.is_(None), n.kind.in_(KINDS), or_(*branches))


def page_parameters(request):
    query = getattr(request, 'query', {})
    from .miniapp_security import AccessError
    try:
        limit = query.get('limit', '12')
        before = query.get('before')
        if (not isinstance(limit, str) or not re.fullmatch(r'[0-9]{1,2}', limit)
                or not 1 <= int(limit) <= 30 or (before is not None and
                    (not isinstance(before, str) or not re.fullmatch(r'[0-9]{1,18}', before) or int(before) <= 0))):
            raise ValueError
        return int(limit), int(before) if before is not None else None
    except (TypeError, ValueError):
        raise AccessError('Некорректная страница уведомлений.', 400) from None
