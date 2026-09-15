from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from ..config import config

SHIFT_START = time(9, 0)
LATE_FINE = 500.0


def shift_now(value=None):
    if value is None:
        value = datetime.now(UTC)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(config.training_timezone))


def is_late(value):
    return shift_now(value).time().replace(tzinfo=None) > SHIFT_START
