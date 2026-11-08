"""Delivery policy; in-app history remains immediate, including quiet hours."""
import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .miniapp_security import AccessError

PREFIX = 'push:preferences:'
DEFAULT = {'quietEnabled': False, 'quietStart': '22:00', 'quietEnd': '08:00'}
URGENT = frozenset({'call', 'booking_assignment', 'booking_reminder', 'reminder',
                   'sale_ready', 'sale_discount', 'service_status', 'control',
                   'attendance', 'attendance_fine', 'operational_alert', 'push_test'})


def preferences(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT) or type(value['quietEnabled']) is not bool:
        raise AccessError('Проверьте настройки тихих часов.', 400)
    for key in ('quietStart', 'quietEnd'):
        if not isinstance(value[key], str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value[key]):
            raise AccessError('Время тихих часов: часы и минуты.', 400)
    if value['quietEnabled'] and value['quietStart'] == value['quietEnd']:
        raise AccessError('Начало и конец тихих часов должны отличаться.', 400)
    return dict(value)


def saved_preferences(raw):
    try:
        return preferences(json.loads(raw))
    except (ValueError, TypeError):
        return dict(DEFAULT)


def quiet_until(value, now):
    if not value['quietEnabled']:
        return None
    local = now.astimezone(ZoneInfo('Europe/Moscow'))
    def minute(raw):
        hour, part = map(int, raw.split(':'))
        return hour * 60 + part
    start, end = minute(value['quietStart']), minute(value['quietEnd'])
    current = local.hour * 60 + local.minute
    active = start <= current < end if start < end else current >= start or current < end
    if not active:
        return None
    target = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(UTC).replace(tzinfo=None)


def push_urgency(kind):
    return 'high' if kind in URGENT else 'low' if kind in {'academy', 'daily_summary', 'manager_advice'} else 'normal'


def event_deadline(notice):
    if notice.kind != 'call':
        return None
    moment = notice.created_at
    if not isinstance(moment, datetime):
        return None
    return (moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)) + timedelta(seconds=60)


def notification_tag(notice, booking_id):
    if type(booking_id) is int and booking_id > 0 and notice.kind in {'booking_assignment', 'booking_reminder', 'reminder', 'sale_ready'}:
        # A move/cancellation replaces the obsolete appointment on the device.
        return 'pb-booking-' + str(booking_id)
    return 'pb-notice-' + str(notice.id)
