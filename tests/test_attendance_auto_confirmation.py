"""Real JPEG and persisted GPS evidence: automatic shift, lunch, review and replay."""
import asyncio
import base64
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_hotel_points_review import POINT, jpeg
from test_workflow_replay import ACTOR, packet, setup

from app.attendance import Attendance
from app.hotel_points import point_key
from app.miniapp_security import AccessError
from app.models import (
    HotelEmployee,
    Notification,
    PayrollEntry,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
    WorkBreak,
)

DAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 6, 15, tzinfo=UTC)


async def prepare():
    engine, factory, work = await setup()
    service = Attendance(work.api)
    service.now = lambda: NOW
    service.send_photo = AsyncMock(return_value='app:/private/shift.jpg')
    work.api.audit_write = AsyncMock()
    work.api.body = AsyncMock(side_effect=lambda r: r['body'])
    work.api.bot = SimpleNamespace()
    async with factory() as session:
        session.add_all([User(id=3, tg_id=3, name='Owner'), UserRole(user_id=3, role='OWNER'),
            User(id=4, tg_id=4, name='Scoped admin'), UserRole(user_id=4, role='ADMIN'),
            User(id=5, tg_id=5, name='Other admin'), UserRole(user_id=5, role='ADMIN'),
            HotelEmployee(user_id=1, hotel_id=1), HotelEmployee(user_id=4, hotel_id=1),
            Setting(key=point_key(1), value=json.dumps(POINT)),
            Shift(user_id=1, hotel_id=1, start_at=NOW.replace(hour=6, minute=0, tzinfo=None),
                  end_at=NOW.replace(hour=15, minute=0, tzinfo=None), status='PLANNED')])
        await session.commit()
    return engine, factory, work, service


async def submit(service, purpose='start', latitude=None, accuracy=10):
    request = {'miniapp_actor': ACTOR}
    service.payload = AsyncMock(return_value={'purpose': purpose, 'date': str(DAY),
        'latitude': POINT['latitude'] if latitude is None else latitude,
        'longitude': POINT['longitude'], 'accuracy': accuracy})
    await service.location(request)
    service.payload.return_value = {'purpose': purpose, 'date': str(DAY),
        'image': 'data:image/jpeg;base64,' + base64.b64encode(jpeg()).decode()}
    return json.loads((await service.photo(request)).text)


def test_fresh_inside_online_shift_confirms_notifies_once_and_lunch_can_finish(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')
    monkeypatch.setattr('app.work_breaks.utc_now', lambda: NOW.replace(tzinfo=None))

    async def run():
        engine, factory, work, service = await prepare()
        try:
            state = await submit(service)
            assert state['start']['status'] == 'STARTED' and state['start']['at']
            assert state['fine'] == 500
            await service.photo({'miniapp_actor': ACTOR})
            assert service.send_photo.await_count == 1
            async with factory() as session:
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                notices = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance')))
                assert {n.user_id for n in notices} == {1, 3, 4}
                assert len(notices) == 3
                assert all(json.loads(n.payload)['status'] == 'STARTED' for n in notices)
            await work.execute(ACTOR, packet('auto-lunch-start', 'break_start', {'hotelId': 1}))
            await work.execute(ACTOR, packet('auto-lunch-end', 'break_end', {'hotelId': 1}))
            service.now = lambda: NOW + timedelta(hours=9)
            finished = await submit(service, 'end')
            assert finished['end']['status'] == 'FINISHED'
            async with factory() as session:
                assert (await session.scalar(select(WorkBreak))).ended_at
                assert (await session.scalar(select(ShiftCheckIn))).full_body_file_id
                assert (await session.scalar(select(ShiftCheckOut))).workplace_file_id
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('location,accuracy', [(POINT['latitude'] + .003, 10), (POINT['latitude'], None)])
def test_outside_or_imprecise_photo_remains_for_leadership_review(monkeypatch, location, accuracy):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            state = await submit(service, latitude=location, accuracy=accuracy)
            assert state['start']['status'] == 'PENDING_REVIEW'
            assert state['start']['at'] is None
            async with factory() as session:
                for kind in ('attendance', 'attendance_fine'):
                    notices = list(await session.scalars(select(Notification).where(Notification.kind == kind)))
                    assert len(notices) == 3 and {row.user_id for row in notices} == {1,3,4}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_recent_confirmed_evidence_can_be_corrected_by_current_scoped_leader(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            await submit(service)
            owner = {'id': 3, 'roles': ['OWNER']}
            # asyncpg returns datetimes; SQLite raw text queries return strings
            # and previously hid the production-only confirmed_at JSON failure.
            original_rows = service.api.rows
            async def postgres_dates(conn, sql, **params):
                rows = await original_rows(conn, sql, **params)
                if 'AS confirmed_at' in sql:
                    return [dict(r) | {'confirmed_at': datetime.fromisoformat(r['confirmed_at'])
                        if isinstance(r['confirmed_at'], str) else r['confirmed_at']} for r in rows]
                return rows
            service.api.rows = postgres_dates
            state = json.loads((await service.pending({'miniapp_actor': owner})).text)
            item = state['items'][0]
            assert item['status'] == 'STARTED' and item['confirmedAt']
            assert 'confirmed_at' not in item
            correction = {'miniapp_actor': owner, 'body': {'userId': 1, 'date': str(DAY),
                'purpose': 'start', 'approved': True, 'expectedAt': item['confirmedAt'],
                'verifiedAt': '2026-09-22T08:55:00+03:00', 'note': 'Уточнено время прибытия по контролю руководителя'}}
            await service.review(correction)
            assert json.loads((await service.review(correction)).text)['alreadyReviewed']
            async with factory() as session:
                row = await session.scalar(select(ShiftCheckIn))
                assert row.started_at.hour == 5 and row.started_at.minute == 55
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                assert (await session.scalar(select(PayrollEntry))).amount == 0
                role = await session.scalar(select(UserRole).where(UserRole.user_id == 3))
                role.role = 'MANAGER'
                await session.commit()
            with pytest.raises(AccessError):
                await service.pending({'miniapp_actor': owner})
            with pytest.raises(AccessError, match='Время уже изменено'):
                await service.review(correction | {'miniapp_actor': {'id': 4, 'roles': ['ADMIN']},
                    'body': correction['body'] | {'verifiedAt': '2026-09-22T08:54:00+03:00'}})
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_zone_changed_while_camera_was_open_requires_review(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        request = {'miniapp_actor': ACTOR}
        try:
            service.payload = AsyncMock(return_value={'purpose': 'start', 'date': str(DAY),
                'latitude': POINT['latitude'], 'longitude': POINT['longitude'], 'accuracy': 10})
            await service.location(request)
            async with factory() as session:
                (await session.get(Setting, point_key(1))).value = json.dumps(POINT | {'latitude': POINT['latitude'] + .003})
                await session.commit()
            service.payload.return_value = {'purpose': 'start', 'date': str(DAY),
                'image': 'data:image/jpeg;base64,' + base64.b64encode(jpeg()).decode()}
            state = json.loads((await service.photo(request)).text)
            assert state['start']['status'] == 'PENDING_REVIEW'
            assert state['start']['geofence']['state'] == 'outside'
        finally:
            await engine.dispose()
    asyncio.run(run())
