import asyncio
import base64
import json
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
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
