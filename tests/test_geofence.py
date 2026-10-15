"""Hotel points distinguish measured inside/outside from uncertain GPS."""

import json
import math
import unittest
from types import SimpleNamespace

import pytest
import test_miniapp_release as baseline
from sqlalchemy import text

from app.miniapp_security import AccessError
from app.people import People
from app.services.geofence import check_hotel_geofence, distance_m, validate_geofence

POINT = {"latitude": 0, "longitude": 0, "attendance_radius_m": 100}


@pytest.mark.parametrize(("lat", "accuracy", "status"), [
    (0.0004, 10, "ACCEPTED"), (0.002, 10, "OUTSIDE"),
    (0.00085, 10, "RETRY"), (0, None, "RETRY"),
    (0, 200, "RETRY"), (1, 200, "RETRY"),
])
def test_inside_outside_and_uncertain_reported_accuracy(lat, accuracy, status):
    result = check_hotel_geofence(POINT, lat, 0, accuracy)
    assert result["status"] == status
    assert result["accepted"] is (status == "ACCEPTED")
    assert result["policy"] == "hotel_radius"
    if status != "ACCEPTED":
        assert "За ошибку геолокации штраф не начисляется" in result["message"]


def test_unconfigured_point_keeps_honest_coordinates_only_policy():
    result = check_hotel_geofence({}, 41.5, 48.1, None)
    assert result["accepted"] and result["status"] == "UNCONFIGURED"
    assert result["policy"] == "coordinates_only" and result["distance_m"] is None


def test_haversine_handles_antimeridian_poles_and_antipodes():
    assert distance_m(0, 179.999, 0, -179.999) == pytest.approx(222.39, abs=0.1)
    assert distance_m(90, 0, 90, 180) == pytest.approx(0, abs=0.001)
    assert distance_m(0, 0, 0, 180) == pytest.approx(math.pi * 6371008.8)
    result = check_hotel_geofence(SimpleNamespace(
        latitude=0, longitude=179.999, attendance_radius_m=500), 0, -179.999, 10)
    assert result["accepted"]


@pytest.mark.parametrize("point", [
    (None, 0, 100), (0, None, 100), (0, 0, None),
    (True, 0, 100), (0, False, 100), (0, 0, True),
    (91, 0, 100), (0, -181, 100), (0, 0, 9), (0, 0, 5001),
    (float("nan"), 0, 100), (0, float("inf"), 100),
])
def test_incomplete_or_invalid_point_cannot_disable_check_silently(point):
    with pytest.raises(ValueError):
        validate_geofence(*point)


@pytest.mark.parametrize("measurement", [(True, 0, 1), (0, 181, 1),
                                         (0, 0, -1), (0, 0, float("inf"))])
def test_invalid_device_measurements_rejected(measurement):
    with pytest.raises(ValueError):
        check_hotel_geofence(POINT, *measurement)


class HotelPointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await baseline.MiniAppTests.asyncSetUp(self)
        self.people = People(self.service)
        with self.engine.inner.begin() as conn:
            for column in ("latitude", "longitude", "attendance_radius_m"):
                conn.execute(text(f"ALTER TABLE hotels ADD COLUMN {column} FLOAT"))

    async def asyncTearDown(self):
        await baseline.MiniAppTests.asyncTearDown(self)

    async def call(self, *, uid=1001, hotel_id=None, body=None):
        path = "/api/miniapp/hotels/points" if hotel_id is None else f"/api/miniapp/hotels/{hotel_id}/point"
        request = baseline.Request(path, uid, "GET" if hotel_id is None else "PUT", body, None)
        if hotel_id is not None:
            request.match_info = {"id": str(hotel_id)}
        response = await self.service.middleware(request, self.people.hotel_points
                                                 if hotel_id is None else self.people.set_hotel_point)
        return response.status, json.loads(response.text)

    async def test_owner_saves_clears_point_and_records_audit(self):
        payload = {"latitude": 41.5, "longitude": 48.1, "radiusM": 100}
        status, data = await self.call(hotel_id=1, body=payload)
        assert status == 200 and data["hotel"]["configured"]
        _, listing = await self.call()
        assert len(listing["items"]) == 2
        assert next(h for h in listing["items"] if h["id"] == 1)["latitude"] == 41.5
        status, data = await self.call(hotel_id=1, body={key: None for key in payload})
        assert status == 200 and not data["hotel"]["configured"]
        with self.engine.inner.connect() as conn:
            logs = conn.execute(text("SELECT details FROM audit_logs WHERE action='hotel_point_changed' ORDER BY id")).scalars().all()
            assert len(logs) == 2
            assert json.loads(logs[0])["after"]["radiusM"] == 100
            assert json.loads(logs[1])["before"]["configured"]

    async def test_admin_only_reads_and_updates_assigned_hotels(self):
        payload = {"latitude": 41.5, "longitude": 48.1, "radiusM": 100}
        status, listing = await self.call(uid=1002)
        assert status == 200 and listing["items"] == []
        assert (await self.call(uid=1002, hotel_id=1, body=payload))[0] == 403
        with self.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO hotel_employees(user_id,hotel_id) VALUES (2,1)"))
        status, listing = await self.call(uid=1002)
        assert status == 200 and [h["id"] for h in listing["items"]] == [1]
        assert (await self.call(uid=1002, hotel_id=1, body=payload))[0] == 200
        assert (await self.call(uid=1002, hotel_id=2, body=payload))[0] == 403

    async def test_non_management_and_invalid_payload_cannot_modify(self):
        payload = {"latitude": 41.5, "longitude": 48.1, "radiusM": 100}
        for uid in (1003, 1004, 1006):
            assert (await self.call(uid=uid))[0] == 403
            assert (await self.call(uid=uid, hotel_id=1, body=payload))[0] == 403
        for body in (payload | {"radiusM": 9}, payload | {"latitude": True},
                     payload | {"longitude": None}, payload | {"userId": 1}):
            assert (await self.call(hotel_id=1, body=body))[0] == 400
        # Cached OWNER role must not survive a server-side role revocation.
        request = baseline.Request("/api/miniapp/hotels/1/point", 1001, "PUT", payload, None)
        request.match_info = {"id": "1"}
        request["miniapp_actor"] = await self.service.actor(request)
        with self.engine.inner.begin() as conn:
            conn.execute(text("DELETE FROM user_roles WHERE user_id=1"))
        with pytest.raises(AccessError):
            await self.people.set_hotel_point(request)
