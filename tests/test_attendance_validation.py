"""Offline validation checks; never call Telegram or write production records."""
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.attendance import Attendance, coordinates, photo_bytes, utc
from app.miniapp_security import AccessError


def payload(**changes):
    data = {"purpose": "start", "date": "2026-09-20", "latitude": 41.5,
            "longitude": 48.1, "accuracy": 12}
    data.update(changes)
    return data


@pytest.mark.parametrize("changes", [
    {"latitude": True}, {"latitude": 91}, {"longitude": -181},
    {"latitude": float("nan")}, {"longitude": float("inf")},
    {"accuracy": -1}, {"accuracy": True}, {"user_id": 123},
])
def test_invalid_location_rejected(changes):
    with pytest.raises(AccessError):
        coordinates(payload(**changes))


def test_unknown_accuracy_does_not_invent_precision():
    assert coordinates(payload(accuracy=None)) == (41.5, 48.1, None)


@pytest.mark.parametrize("image", [None, "", "data:image/png;base64,AA==",
                                  "data:image/jpeg;base64,@@@@", "data:image/jpeg;base64,AA=="])
def test_invalid_photo_rejected(image):
    with pytest.raises(AccessError):
        photo_bytes({"purpose": "start", "date": "2026-09-20", "image": image})


def test_utc_normalization():
    expected = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)
    assert utc("2026-09-20T06:00:00") == expected
    assert utc("2026-09-20T09:00:00+03:00") == expected


@pytest.mark.parametrize("roles,eligible", [
    (["OWNER"], False), (["ADMIN"], False), (["MANAGER"], True),
    (["PHOTOGRAPHER"], True), (["OWNER", "PHOTOGRAPHER"], True),
])
def test_status_is_honest_about_roles_and_geofencing(roles, eligible):
    service = Attendance(SimpleNamespace(tz=ZoneInfo("Europe/Moscow")))
    result = service.result({"roles": roles}, None, None,
                            datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert result["eligible"] is eligible
    assert result["geoPolicy"] == "coordinates_only"
    assert result["start"] is None and result["end"] is None
    assert result["fine"] == 0
