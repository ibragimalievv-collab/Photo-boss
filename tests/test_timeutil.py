from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import timeutil
from app.timeutil import as_utc, business_tz, naive_utc


def test_stored_naive_aware_and_text_values_mean_the_same_utc_moment():
    moment = datetime(2026, 9, 1, 9, 30, tzinfo=UTC)
    moscow = moment.astimezone(timezone(timedelta(hours=3)))
    stored = moment.replace(tzinfo=None)  # how the database keeps it
    assert as_utc(stored) == moment
    assert as_utc(moscow) == moment and as_utc(moscow).tzinfo is UTC
    assert as_utc('2026-09-01T12:30:00+03:00') == moment
    assert as_utc('2026-09-01 09:30:00') == moment
    assert naive_utc(moscow) == stored


def test_business_zone_comes_from_training_timezone(monkeypatch):
    monkeypatch.delenv('TRAINING_TIMEZONE', raising=False)
    assert business_tz() == ZoneInfo('Europe/Moscow')
    monkeypatch.setenv('TRAINING_TIMEZONE', ' Europe/Samara ')
    assert business_tz() == ZoneInfo('Europe/Samara')
    assert timeutil.business_tz() is business_tz()  # one zone object per name
