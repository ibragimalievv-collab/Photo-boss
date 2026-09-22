import asyncio
import json
from datetime import date, time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.operations import backup_payload


def test_export_preserves_booking_time_and_numeric_money():
    booking = SimpleNamespace(id=1, hotel_id=1, client_id=1, room="101", guest_count=2,
                              deposit=1234.56, shoot_date=date(2026, 9, 22),
                              shoot_time=time(12, 30, 15), manager_id=1,
                              photographer_id=None, status="NEW")
    session = SimpleNamespace(scalars=AsyncMock(side_effect=[
        SimpleNamespace(all=list), SimpleNamespace(all=list),
        SimpleNamespace(all=lambda: [booking]), SimpleNamespace(all=list),
    ]))
    document = json.loads(asyncio.run(backup_payload(session)))
    assert document["bookings"][0]["shoot_time"] == "12:30:15"
    assert document["bookings"][0]["shoot_date"] == "2026-09-22"
    assert document["bookings"][0]["deposit"] == 1234.56
    assert document["bookings"][0]["photographer_id"] is None
    assert document["version"] == 1
    assert set(document) == {"created_at", "version", "users", "clients", "bookings", "sales"}
