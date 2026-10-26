import asyncio
import base64
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_hotel_points_review import jpeg
from test_workflow_replay import ACTOR, packet, setup

from app.attendance import Attendance
from app.miniapp_security import AccessError
from app.models import (
    Hotel,
    HotelEmployee,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
    WorkBreak,
)
from app.work_breaks import WorkBreaks

DAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 8, tzinfo=UTC)


def test_lunch_defaults_to_single_planned_hotel_and_requires_choice_for_multiple_plans():
    async def run():
        engine, factory, work = await setup()
        try:
            async with factory() as session:
                session.add(Hotel(id=2, name='Planned hotel'))
                await session.flush()
                session.add_all([HotelEmployee(user_id=1, hotel_id=1), HotelEmployee(user_id=1, hotel_id=2),
                    Shift(user_id=1, hotel_id=2, start_at=NOW.replace(hour=6, tzinfo=None),
                        end_at=NOW.replace(hour=18, tzinfo=None), status='PLANNED')])
                await session.commit()
            request = {'miniapp_actor': ACTOR}
            first = json.loads((await WorkBreaks(work.api).listing(request)).text)
            assert len(first['hotels']) == 2 and first['defaultHotelId'] == 2
            async with factory() as session:
                session.add(Shift(user_id=1, hotel_id=1, start_at=NOW.replace(hour=6, tzinfo=None),
                    end_at=NOW.replace(hour=18, tzinfo=None), status='PLANNED'))
                await session.commit()
            assert json.loads((await WorkBreaks(work.api).listing(request)).text)['defaultHotelId'] is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_pending_start_allows_lunch_return_and_online_end_without_approving_evidence(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')
    monkeypatch.setattr('app.work_breaks.utc_now', lambda: NOW.replace(tzinfo=None))

    async def run():
        engine, factory, work = await setup()
        service = Attendance(work.api)
        service.now = lambda: NOW
        service.send_photo = AsyncMock(return_value='app:/saved-end.jpg')
        work.api.audit_write = AsyncMock()
        try:
            async with factory() as session:
                session.add_all([HotelEmployee(user_id=1, hotel_id=1), ShiftCheckIn(
                    user_id=1, shift_date=DAY, status='PENDING_REVIEW',
                    latitude=44.9, longitude=37.3, full_body_file_id='app:/saved-start.jpg',
                    offline_claimed_at=NOW.replace(tzinfo=None))])
                await session.commit()
            request = {'miniapp_actor': ACTOR}
            state = json.loads((await WorkBreaks(work.api).listing(request)).text)
            assert state['canStart'] and state['startPendingReview']
            lunch = packet('pending-lunch-start', 'break_start', {'hotelId': 1})
            first = json.loads((await work.execute(ACTOR, lunch)).text)
            assert first == json.loads((await work.execute(ACTOR, lunch)).text)
            returned = packet('pending-lunch-return', 'break_end', {'hotelId': 1})
            await work.execute(ACTOR, returned)
            assert json.loads((await WorkBreaks(work.api).listing(request)).text)['items'][0]['endedAt']
            service.payload = AsyncMock(return_value={'purpose': 'end', 'date': str(DAY),
                'latitude': 44.9, 'longitude': 37.3, 'accuracy': 10})
            await service.location(request)
            service.payload.return_value = {'purpose': 'end', 'date': str(DAY),
                'image': 'data:image/jpeg;base64,'+base64.b64encode(jpeg()).decode()}
            await service.photo(request)
            await service.photo(request)
            assert service.send_photo.await_count == 1
            state = json.loads((await WorkBreaks(work.api).listing(request)).text)
            assert not state['canStart']
            with pytest.raises(AccessError):
                await work.execute(ACTOR, packet('lunch-after-end', 'break_start', {'hotelId': 1}))
            async with factory() as session:
                start = await session.scalar(select(ShiftCheckIn))
                end = await session.scalar(select(ShiftCheckOut))
                assert start.status == end.status == 'PENDING_REVIEW'
                assert start.started_at is None and end.ended_at is None
                assert (await session.scalar(select(WorkBreak))).ended_at is not None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_incomplete_or_rejected_start_does_not_unlock_lunch():
    async def run():
        engine, factory, work = await setup()
        try:
            async with factory() as session:
                session.add_all([HotelEmployee(user_id=1, hotel_id=1), ShiftCheckIn(
                    user_id=1, shift_date=DAY, status='PENDING_REVIEW')])
                await session.commit()
            with pytest.raises(AccessError):
                await work.execute(ACTOR, packet('lunch-no-evidence', 'break_start', {'hotelId': 1}))
            state = json.loads((await WorkBreaks(work.api).listing({'miniapp_actor': ACTOR})).text)
            assert not state['canStart']
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_planned_hotel_full_shift_lunch_and_leadership_review_are_persisted_once(monkeypatch):
    """Exercise real server actions, including evidence still awaiting review."""
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '1')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')
    clock = [NOW.replace(hour=5, minute=55)]
    monkeypatch.setattr('app.work_breaks.utc_now', lambda: clock[0].replace(tzinfo=None))

    async def run():
        engine, factory, work = await setup()
        work.api.bot = SimpleNamespace()
        work.api.audit_write = AsyncMock()
        work.api.body = AsyncMock(side_effect=lambda request: request['body'])
        service = Attendance(work.api)
        service.now = lambda: clock[0]
        service.send_photo = AsyncMock(side_effect=['app:/start.jpg', 'app:/end.jpg'])
        employee = {'miniapp_actor': ACTOR}
        owner = {'miniapp_actor': {'id': 3, 'roles': ['OWNER']}}
        try:
            async with factory() as session:
                session.add_all([Hotel(id=2, name='Planned hotel'), User(id=3, tg_id=3, name='Owner')])
                await session.flush()
                session.add_all([UserRole(user_id=3, role='OWNER'),
                    HotelEmployee(user_id=1, hotel_id=1), HotelEmployee(user_id=1, hotel_id=2),
                    Shift(user_id=1, hotel_id=2, start_at=clock[0].replace(tzinfo=None),
                        end_at=clock[0].replace(hour=18, tzinfo=None), status='PLANNED')])
                await session.commit()

            async def submit(purpose):
                service.payload = AsyncMock(return_value={'purpose': purpose, 'date': str(DAY),
                    'latitude': 44.9, 'longitude': 37.3, 'accuracy': 10})
                await service.location(employee)
                service.payload.return_value = {'purpose': purpose, 'date': str(DAY),
                    'image': 'data:image/jpeg;base64,' + base64.b64encode(jpeg()).decode()}
                result = json.loads((await service.photo(employee)).text)
                assert result == json.loads((await service.photo(employee)).text)
                return result

            assert (await submit('start'))['start']['status'] == 'PENDING_REVIEW'
            state = json.loads((await WorkBreaks(work.api).listing(employee)).text)
            assert state['canStart'] and state['defaultHotelId'] == 2
            clock[0] += timedelta(hours=3)
            lunch = packet('cycle-lunch-start', 'break_start', {'hotelId': state['defaultHotelId']})
            first = json.loads((await work.execute(ACTOR, lunch)).text)
            assert first == json.loads((await work.execute(ACTOR, lunch)).text)
            clock[0] += timedelta(minutes=35)
            returned = packet('cycle-lunch-return', 'break_end', {'hotelId': 2})
            await work.execute(ACTOR, returned)
            await work.execute(ACTOR, returned)
            lunch_state = json.loads((await WorkBreaks(work.api).listing(employee)).text)
            assert lunch_state['items'][0]['hotelId'] == 2
            assert lunch_state['items'][0]['minutes'] == 35

            clock[0] = NOW.replace(hour=15)
            assert (await submit('end'))['end']['status'] == 'PENDING_REVIEW'
            assert not json.loads((await WorkBreaks(work.api).listing(employee)).text)['canStart']
            listing = json.loads((await service.pending(owner)).text)
            assert {row['purpose'] for row in listing['items']} == {'start', 'end'}
            assert all(row['offline_claimed_at'] and row['location_received_at'] for row in listing['items'])
            for purpose, when in [('start', '2026-09-22T08:55:00+03:00'), ('end', '2026-09-22T18:00:00+03:00')]:
                decision = owner | {'body': {'userId': 1, 'date': str(DAY), 'purpose': purpose,
                    'approved': True, 'verifiedAt': when, 'note': 'Фото и время проверены руководителем'}}
                await service.review(decision)
                assert json.loads((await service.review(decision)).text)['alreadyReviewed']
            listing = json.loads((await service.pending(owner)).text)
            assert all(row['confirmedAt'] for row in listing['items'])
            assert service.send_photo.await_count == 2
            async with factory() as session:
                for model in (ShiftCheckIn, ShiftCheckOut, WorkBreak):
                    assert await session.scalar(select(func.count(model.id))) == 1
                start = await session.scalar(select(ShiftCheckIn))
                end = await session.scalar(select(ShiftCheckOut))
                lunch = await session.scalar(select(WorkBreak))
                assert start.status == 'STARTED' and end.status == 'FINISHED'
                assert end.ended_at - start.started_at == timedelta(hours=9, minutes=5)
                assert lunch.ended_at - lunch.started_at == timedelta(minutes=35)
        finally:
            await engine.dispose()
    asyncio.run(run())
