"""Delayed shift storage must leave the DB pool available to other employees."""
import asyncio
import base64
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import test_workflow_services
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from test_attendance_auto_confirmation import DAY, NOW, prepare
from test_hotel_points_review import POINT, jpeg
from test_workflow_replay import ACTOR, packet
from test_workflow_services import booking_data

from app.miniapp_security import AccessError
from app.models import Booking, Notification, ShiftCheckIn, User


def photo_request(service, purpose):
    service.payload = AsyncMock(return_value={
        'purpose': purpose, 'date': str(DAY),
        'image': 'data:image/jpeg;base64,' + base64.b64encode(jpeg()).decode(),
    })
    return {'miniapp_actor': ACTOR}


async def location(service, purpose, **changes):
    service.payload = AsyncMock(return_value={
        'purpose': purpose, 'date': str(DAY), 'latitude': POINT['latitude'],
        'longitude': POINT['longitude'], 'accuracy': 10,
    } | changes)
    await service.location({'miniapp_actor': ACTOR})


async def pooled_fixture(monkeypatch, tmp_path):
    # A single actual connection makes starvation deterministic: no load
    # generator, race-prone sleep, or production database is needed.
    engine = create_async_engine(
        f'sqlite+aiosqlite:///{tmp_path / "shift-uploads.sqlite"}',
        pool_size=1, max_overflow=0, pool_timeout=1,
    )
    monkeypatch.setattr(test_workflow_services, 'create_async_engine', lambda *_a, **_kw: engine)
    return await prepare()


def delayed_storage(service):
    entered, release = asyncio.Event(), asyncio.Event()

    async def store(*_args):
        entered.set()
        await release.wait()
        return 'app:/private/slow-shift.jpg'

    service.send_photo = AsyncMock(side_effect=store)
    return entered, release


@pytest.mark.parametrize('gps', [True, False])
@pytest.mark.parametrize('purpose', ['start', 'end'])
def test_booking_can_commit_while_shift_photo_storage_waits(monkeypatch, tmp_path, gps, purpose):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1' if gps else '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, work, service = await pooled_fixture(monkeypatch, tmp_path)
        task = None
        try:
            if purpose == 'end':
                if gps:
                    await location(service, 'start')
                await service.photo(photo_request(service, 'start'))
                service.now = lambda: NOW + timedelta(hours=9)
            if gps:
                await location(service, purpose)
            entered, release = delayed_storage(service)
            task = asyncio.create_task(service.photo(photo_request(service, purpose)))
            await asyncio.wait_for(entered.wait(), timeout=2)
            # Real booking work must commit while the upload is still blocked.
            result = await asyncio.wait_for(work.execute(ACTOR, packet('during-shift-upload', 'booking',
                booking_data() | {'photographer_id': None})), timeout=2)
            assert json.loads(result.text)['bookingId'] > 0
            assert not task.done()
            release.set()
            state = json.loads((await task).text)
            assert state[purpose]['status'] == ('STARTED' if purpose == 'start' else 'FINISHED')
            async with factory() as session:
                assert await session.scalar(select(func.count(Booking.id))) == 1
        finally:
            if task and not task.done():
                release.set()
                await task
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize('gps', [True, False])
def test_access_revoked_during_upload_never_records_shift(monkeypatch, tmp_path, gps):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1' if gps else '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await pooled_fixture(monkeypatch, tmp_path)
        task = None
        try:
            if gps:
                await location(service, 'start')
            entered, release = delayed_storage(service)
            task = asyncio.create_task(service.photo(photo_request(service, 'start')))
            await asyncio.wait_for(entered.wait(), timeout=2)
            async with factory() as session:
                (await session.get(User, 1)).active = False
                await session.commit()
            release.set()
            with pytest.raises(AccessError) as error:
                await task
            assert error.value.status == 403
            async with factory() as session:
                row = await session.scalar(select(ShiftCheckIn))
                assert row is None or (row.status == 'AWAITING_PHOTO' and not row.full_body_file_id)
                assert await session.scalar(select(func.count(Notification.id))) == 0
        finally:
            if task and not task.done():
                release.set()
                await task
            await engine.dispose()

    asyncio.run(run())


def test_new_gps_while_upload_waits_cannot_be_overwritten_by_old_photo(monkeypatch, tmp_path):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await pooled_fixture(monkeypatch, tmp_path)
        task = None
        try:
            await location(service, 'start')
            entered, release = delayed_storage(service)
            task = asyncio.create_task(service.photo(photo_request(service, 'start')))
            await asyncio.wait_for(entered.wait(), timeout=2)
            await location(service, 'start', latitude=POINT['latitude'] + .01)
            release.set()
            with pytest.raises(AccessError, match='Данные смены изменились') as error:
                await task
            assert error.value.status == 409
            async with factory() as session:
                row = await session.scalar(select(ShiftCheckIn))
                assert row.latitude == POINT['latitude'] + .01
                assert row.status == 'AWAITING_PHOTO' and not row.full_body_file_id
                assert await session.scalar(select(func.count(Notification.id))) == 0
        finally:
            if task and not task.done():
                release.set()
                await task
            await engine.dispose()

    asyncio.run(run())
