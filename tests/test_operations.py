import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["BOT_TOKEN"] = "123456:TEST_ONLY_DO_NOT_USE_FOR_TELEGRAM"
os.environ["ADMIN_TELEGRAM_IDS"] = "5000000001"
os.environ["PHOTOGRAPHER_PERCENT"] = "11"
os.environ["MANAGER_PERCENT"] = "16"
os.environ["OPENAI_API_KEY"] = ""

from app.config import config
from app.services.operations import (
    photographer_score,
    reconciliation_status,
    reminder_kind,
)


class BookingStub:
    shoot_date = date(2026, 9, 21)
    shoot_time = time(12, 0)


def test_reminders_fire_only_in_expected_windows():
    booking = BookingStub()
    shoot = datetime(2026, 9, 21, 12, tzinfo=ZoneInfo(config.training_timezone))
    assert reminder_kind(booking, shoot - timedelta(hours=24)) == "24H"
    assert reminder_kind(booking, shoot - timedelta(hours=2)) == "2H"
    assert reminder_kind(booking, shoot - timedelta(hours=6)) is None
    assert reminder_kind(booking, shoot + timedelta(minutes=1)) is None


def test_smart_assignment_prefers_low_load_and_relevant_hotel():
    best = photographer_score(
        same_hotel=True, bookings_today=0, quality=8, distance_minutes=0
    )
    busy = photographer_score(
        same_hotel=True, bookings_today=3, quality=10, distance_minutes=0
    )
    remote = photographer_score(
        same_hotel=False, bookings_today=0, quality=8, distance_minutes=60
    )
    assert best > busy
    assert best > remote


def test_bank_reconciliation_detects_amount_mismatch():
    assert reconciliation_status("1500.00", "1500") == "MATCHED"
    assert reconciliation_status("1500.00", "1499.99") == "MISMATCH"
