"""GPS stays mandatory while hotel reference zones are configured separately."""
import asyncio
import base64
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select
from test_attendance_auto_confirmation import DAY, NOW, prepare, submit
from test_hotel_points_review import jpeg
from test_workflow_replay import ACTOR

from app.attendance import geofence_required
from app.hotel_points import HotelPoints, point_key
from app.miniapp_security import AccessError
from app.models import Notification, PayrollEntry, Setting, ShiftCheckIn, ShiftCheckOut


def set_photo(service, purpose='start'):
    service.payload = AsyncMock(return_value={'purpose': purpose, 'date': str(DAY),
        'image': 'data:image/jpeg;base64,' + base64.b64encode(jpeg()).decode()})


async def location(service, *, purpose='start', latitude=44.9, longitude=37.3, accuracy=26):
    service.payload = AsyncMock(return_value={'purpose': purpose, 'date': str(DAY),
        'latitude': latitude, 'longitude': longitude, 'accuracy': accuracy})
    return json.loads((await service.location({'miniapp_actor': ACTOR})).text)


def policy(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')


def test_zone_comparison_is_strict_by_default_and_explicitly_reversible(monkeypatch):
    monkeypatch.delenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', raising=False)
    assert geofence_required()
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '0')
    assert not geofence_required()
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '1')
    assert geofence_required()


@pytest.mark.parametrize('configured', [False, True])
def test_fresh_gps_and_new_photo_confirm_without_claiming_hotel_distance(monkeypatch, configured):
    policy(monkeypatch)

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            if not configured:
                async with factory() as session:
                    await session.execute(delete(Setting).where(Setting.key == point_key(1)))
                    await session.commit()
            # These reported coordinates differ from the fixture hotel point;
            # zone comparison is explicitly disabled, GPS recording is not.
            state = await submit(service, latitude=44.9, accuracy=26)
            assert state['gpsRequired'] is True and state['geofenceRequired'] is False
            assert state['geoPolicy'] == 'gps_only'
            assert state['start']['status'] == 'STARTED'
            assert state['start']['locationSaved'] and state['start']['locationFresh']
            context = state['start']['geofence']
            assert context['state'] == 'gps_only' and context['geofenceRequired'] is False
            assert 'distanceM' not in context and 'radiusM' not in context
            await service.photo({'miniapp_actor': ACTOR})
            assert service.send_photo.await_count == 1
            service.now = lambda: NOW + timedelta(hours=9)
            finished = await submit(service, 'end', latitude=44.9, accuracy=26)
            assert finished['end']['status'] == 'FINISHED'
            async with factory() as session:
                for model in (ShiftCheckIn, ShiftCheckOut):
                    row = await session.scalar(select(model))
                    assert row.latitude == 44.9 and row.longitude is not None
                    assert row.location_received_at is not None
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                notices = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance')))
                assert len(notices) == 6 and {n.user_id for n in notices} == {1, 3, 4}
                assert all('расстояние до отеля пока не проверялось' in n.text for n in notices)
            points = json.loads((await HotelPoints(service.api).listing(
                {'miniapp_actor': {'id': 3, 'roles': ['OWNER']}})).text)
            assert points['gpsRequired'] is True and points['geofenceRequired'] is False
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_missing_stale_or_invalid_gps_still_cannot_confirm(monkeypatch):
    policy(monkeypatch)

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            set_photo(service)
            with pytest.raises(AccessError, match='Сначала отправьте геолокацию'):
                await service.photo({'miniapp_actor': ACTOR})
            with pytest.raises(AccessError, match='некорректные координаты'):
                await location(service, latitude=None)
            await location(service)
            service.now = lambda: NOW + timedelta(seconds=301)
            set_photo(service)
            with pytest.raises(AccessError, match='Геолокация устарела'):
                await service.photo({'miniapp_actor': ACTOR})
            service.send_photo.assert_not_awaited()
            async with factory() as session:
                assert (await session.scalar(select(ShiftCheckIn))).status == 'AWAITING_PHOTO'
                assert await session.scalar(select(func.count(Notification.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_switch_get_or_old_photo_cannot_approve_pending_fresh_gps_and_photo_can(monkeypatch):
    policy(monkeypatch)
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            original = NOW.replace(hour=5, minute=55)
            service.now = lambda: original
            pending = await submit(service, latitude=0)
            assert pending['start']['status'] == 'PENDING_REVIEW'
            assert pending['fine'] == 0
            monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '0')
            service.now = lambda: NOW + timedelta(hours=2)
            assert json.loads((await service.status({'miniapp_actor': ACTOR})).text)['start']['status'] == 'PENDING_REVIEW'
            set_photo(service)
            assert json.loads((await service.photo({'miniapp_actor': ACTOR})).text)['start']['status'] == 'PENDING_REVIEW'
            assert service.send_photo.await_count == 1
            refreshed = await location(service)
            assert refreshed['start']['status'] == 'PENDING_REVIEW'
            assert refreshed['start']['geofence']['recovery']['trustedOnlineReceivedAt'] == original.isoformat()
            # A repeated location request cannot erase the original trusted time.
            await location(service)
            service.send_photo.return_value = 'app:/fresh-camera-retry.jpg'
            set_photo(service)
            confirmed = json.loads((await service.photo({'miniapp_actor': ACTOR})).text)
            assert confirmed['start']['status'] == 'STARTED' and confirmed['fine'] == 0
            assert confirmed['start']['at'] == original.astimezone(service.api.tz).isoformat()
            await service.photo({'miniapp_actor': ACTOR})
            assert service.send_photo.await_count == 2
            async with factory() as session:
                assert await session.scalar(select(func.count(ShiftCheckIn.id))) == 1
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
                row = await session.scalar(select(ShiftCheckIn))
                assert row.full_body_file_id == 'app:/fresh-camera-retry.jpg'
                assert row.latitude == 44.9 and row.location_received_at is not None
                active = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance', Notification.resolved_at.is_(None))))
                assert len(active) == 3 and all(json.loads(n.payload)['status'] == 'STARTED' for n in active)
            audit = [call.args for call in service.api.audit_write.await_args_list if call.args[2] == 'attendance_location_resubmitted']
            assert json.loads(audit[0][5])['previousPhoto'] == 'app:/private/shift.jpg'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_pending_recovery_gps_can_expire_and_restoring_zone_policy_stays_strict(monkeypatch):
    policy(monkeypatch)
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            await submit(service, latitude=0)
            monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '0')
            await location(service)
            service.now = lambda: NOW + timedelta(seconds=301)
            set_photo(service)
            with pytest.raises(AccessError, match='Геолокация устарела'):
                await service.photo({'miniapp_actor': ACTOR})
            monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '1')
            with pytest.raises(AccessError, match='ожидает проверки'):
                await location(service)
            async with factory() as session:
                assert (await session.scalar(select(ShiftCheckIn))).status == 'PENDING_REVIEW'
            assert service.send_photo.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_offline_stays_manual_and_null_coordinates_are_rejected_even_without_zones(monkeypatch):
    policy(monkeypatch)

    async def run():
        engine, factory, work, service = await prepare()
        try:
            data = {'purpose': 'start', 'claimedAt': NOW.isoformat(),
                'latitude': 44.9, 'longitude': 37.3, 'accuracy': 26}
            with pytest.raises(AccessError, match='некорректные координаты'):
                async with work.api.engine.begin() as conn:
                    await service.offline(conn, ACTOR, DAY, data | {'latitude': None, 'longitude': None, 'accuracy': None}, jpeg())
            async with work.api.engine.begin() as conn:
                result = await service.offline(conn, ACTOR, DAY, data, jpeg())
            assert result['attendanceStatus'] == 'PENDING_REVIEW'
            with pytest.raises(AccessError, match='ожидает проверки'):
                await location(service)
            set_photo(service)
            state = json.loads((await service.photo({'miniapp_actor': ACTOR})).text)
            assert state['start']['status'] == 'PENDING_REVIEW'
            assert state['start']['evidenceSource'] == 'offline'
            assert service.send_photo.await_count == 1
            async with factory() as session:
                row = await session.scalar(select(ShiftCheckIn))
                assert row.started_at is None and row.latitude == 44.9
        finally:
            await engine.dispose()
    asyncio.run(run())
