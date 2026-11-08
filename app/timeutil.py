"""One place for time conversions.

Datetimes are stored naive in the database and mean UTC. The business works on
local time of the configured zone (TRAINING_TIMEZONE, Europe/Moscow by default).
"""
import os
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo


def as_utc(value):
    """An aware UTC datetime from a stored naive UTC value, an aware value or ISO text."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def naive_utc(value):
    """The database form: naive, meaning UTC."""
    return as_utc(value).replace(tzinfo=None)


@lru_cache(maxsize=8)
def _zone(name):
    return ZoneInfo(name)


def business_tz():
    """Local time of the business; validated at startup by app.config."""
    return _zone((os.getenv('TRAINING_TIMEZONE') or 'Europe/Moscow').strip())
