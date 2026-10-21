"""Temporary owner-authorized photo-only attendance, reversible to strict GPS."""
import asyncio
import base64
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_attendance_auto_confirmation import DAY, NOW, prepare, submit
from test_hotel_points_review import jpeg
from test_workflow_replay import ACTOR

from app.attendance import gps_required
from app.hotel_points import HotelPoints
from app.miniapp_security import AccessError
from app.models import Notification, PayrollEntry, ShiftCheckIn, ShiftCheckOut
from app.work_breaks import WorkBreaks


def photo_request(service, purpose='start', image=None):
    service.payload = AsyncMock(return_value={'purpose': purpose, 'date': str(DAY),
        'image': image or 'data:image/jpeg;base64,' + base64.b64encode(jpeg()).decode()})
    return {'miniapp_actor': ACTOR}


def test_gps_is_strict_unless_explicitly_disabled(monkeypatch):
    monkeypatch.delenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', raising=False)
    assert gps_required()
    for value in ('1', '', 'false', 'unexpected'):
        monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', value)
        assert gps_required()
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '0')
    assert not gps_required()


def test_new_online_start_and_end_need_valid_photo_but_no_location_and_notify_once(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            started = json.loads((await service.photo(photo_request(service))).text)
            assert started['gpsRequired'] is False and started['geoPolicy'] == 'temporarily_disabled'
            assert started['start']['status'] == 'STARTED' and started['start']['photoSaved']
            assert not started['start']['locationSaved'] and not started['start']['locationFresh']
            assert started['start']['geofence']['state'] == 'temporarily_disabled'
            assert started['start']['evidenceSource'] == 'online'
            assert started['fine'] == 500
            await service.photo({'miniapp_actor': ACTOR})
            assert service.send_photo.await_count == 1
            service.now = lambda: NOW + timedelta(hours=9)
            ended = json.loads((await service.photo(photo_request(service, 'end'))).text)
            assert ended['end']['status'] == 'FINISHED' and ended['end']['photoSaved']
            await service.photo({'miniapp_actor': ACTOR})
            assert service.send_photo.await_count == 2
            async with factory() as session:
                for model in (ShiftCheckIn, ShiftCheckOut):
                    row = await session.scalar(select(model))
                    assert row.latitude is None and row.longitude is None and row.location_received_at is None
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                notices = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance')))
                assert len(notices) == 6 and {n.user_id for n in notices} == {1, 3, 4}
                assert all('GPS временно отключён' in row.text and 'местоположение не проверялось' in row.text for row in notices)
            hotel_state = json.loads((await HotelPoints(service.api).listing(
                {'miniapp_actor': {'id': 3, 'roles': ['OWNER']}})).text)
            assert hotel_state['gpsRequired'] is False
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_turning_gps_back_on_enforces_location_for_new_photo_submission(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, _factory, _work, service = await prepare()
        try:
            await service.photo(photo_request(service))
            monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1')
            with pytest.raises(AccessError, match='Сначала отправьте геолокацию'):
                await service.photo(photo_request(service, 'end'))
            assert service.send_photo.await_count == 1
            status = json.loads((await service.status({'miniapp_actor': ACTOR})).text)
            assert status['gpsRequired'] is True
            assert status['start']['status'] == 'STARTED'
            assert status['start']['geofence']['gpsRequired'] is False
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_switch_alone_does_not_approve_old_pending_new_photo_preserves_online_time(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            original = NOW.replace(hour=5, minute=55)
            service.now = lambda: original
            pending = await submit(service, latitude=0)
            assert pending['start']['status'] == 'PENDING_REVIEW' and pending['fine'] == 0
            monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '0')
            service.now = lambda: NOW + timedelta(hours=2)
            status = json.loads((await service.status({'miniapp_actor': ACTOR})).text)
            assert status['start']['status'] == 'PENDING_REVIEW'
            service.send_photo.return_value = 'app:/new-private/photo.jpg'
            result = json.loads((await service.photo(photo_request(service))).text)
            assert result['start']['status'] == 'STARTED' and result['fine'] == 0
            assert result['start']['at'] == original.astimezone(service.api.tz).isoformat()
            await service.photo({'miniapp_actor': ACTOR})
            assert service.send_photo.await_count == 2
            async with factory() as session:
                assert await session.scalar(select(func.count(ShiftCheckIn.id))) == 1
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
                assert (await session.scalar(select(ShiftCheckIn))).full_body_file_id == 'app:/new-private/photo.jpg'
                active = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance', Notification.resolved_at.is_(None))))
                assert len(active) == 3
                assert all(json.loads(row.payload)['status'] == 'STARTED' for row in active)
            audit = [call.args for call in service.api.audit_write.await_args_list if call.args[2] == 'attendance_photo_resubmitted']
            assert json.loads(audit[0][5])['previousPhoto'] == 'app:/private/shift.jpg'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_incomplete_legacy_pending_uses_new_server_time_instead_of_old_claim(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            async with factory() as session:
                session.add(ShiftCheckIn(user_id=1, shift_date=DAY, status='PENDING_REVIEW',
                    offline_claimed_at=NOW.replace(hour=5, minute=55, tzinfo=None)))
                await session.commit()
            state = json.loads((await service.photo(photo_request(service))).text)
            assert state['start']['status'] == 'STARTED' and state['fine'] == 500
            assert state['start']['at'] == NOW.astimezone(service.api.tz).isoformat()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_no_gps_still_decodes_jpeg_and_rolls_back_failed_storage(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '0')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            fake = 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8\xff' + b'bad' * 100 + b'\xff\xd9').decode()
            with pytest.raises(AccessError, match='повреждена'):
                await service.photo(photo_request(service, image=fake))
            service.send_photo.assert_not_awaited()
            service.send_photo.side_effect = AccessError('Сохранение фото не подтверждено', 503)
            with pytest.raises(AccessError):
                await service.photo(photo_request(service))
            async with factory() as session:
                assert await session.scalar(select(func.count(ShiftCheckIn.id))) == 0
                assert await session.scalar(select(func.count(Notification.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_offline_null_gps_stays_manual_and_cannot_be_autoapproved_by_new_photo(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, work, service = await prepare()
        try:
            data = {'purpose': 'start', 'claimedAt': NOW.replace(hour=5, minute=55).isoformat(),
                'latitude': None, 'longitude': None, 'accuracy': None}
            async with work.api.engine.begin() as conn:
                result = await service.offline(conn, ACTOR, DAY, data, jpeg())
            assert result['attendanceStatus'] == 'PENDING_REVIEW'
            state = json.loads((await service.status({'miniapp_actor': ACTOR})).text)
            assert state['start']['evidenceSource'] == 'offline'
            assert state['start']['at'] is None and not state['start']['locationSaved']
            assert json.loads((await WorkBreaks(work.api).listing({'miniapp_actor': ACTOR})).text)['canStart']
            with pytest.raises(AccessError, match='Офлайн-отметка ожидает проверки'):
                await service.photo(photo_request(service))
            assert service.send_photo.await_count == 1
            monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1')
            with pytest.raises(AccessError, match='некорректные координаты'):
                async with work.api.engine.begin() as conn:
                    await service.offline(conn, ACTOR, DAY, data, jpeg())
            monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '0')
            # Explicit leadership verification remains possible after strict
            # GPS is restored; this does not claim the saved photo had GPS.
            request = {'miniapp_actor': {'id': 3, 'roles': ['OWNER']}, 'body': {
                'userId': 1, 'date': str(DAY), 'purpose': 'start', 'approved': True,
                'verifiedAt': NOW.replace(hour=5, minute=55).isoformat(),
                'note': 'Проверено руководителем в режиме временного отключения GPS'}}
            await service.review(request)
            async with factory() as session:
                assert (await session.scalar(select(ShiftCheckIn))).status == 'STARTED'
        finally:
            await engine.dispose()
    asyncio.run(run())
