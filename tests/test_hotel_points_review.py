import asyncio
import base64
import json
from datetime import date, datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import func, select
from test_workflow_replay import ACTOR, setup

from app.attendance import Attendance, photo_bytes
from app.hotel_points import (
    HotelPoints,
    capture_context,
    classify,
    context_key,
    point_key,
    point_payload,
    read_json,
    revision,
    startup_hotel_point,
)
from app.miniapp_security import AccessError
from app.models import (
    Hotel,
    HotelEmployee,
    PayrollEntry,
    Setting,
    ShiftCheckIn,
    User,
    UserRole,
)

POINT = {'address': 'Пионерский проспект, 253/1, Анапа', 'latitude': 44.979340,
         'longitude': 37.265541, 'radiusM': 200}
NOW = datetime(2026, 9, 22, 6, 15, tzinfo=timezone.utc)


def jpeg():
    output = BytesIO()
    Image.new('RGB', (320, 480), '#558866').save(output, 'JPEG')
    return output.getvalue()


@pytest.mark.parametrize('lat,accuracy,state', [
    (POINT['latitude'], 10, 'inside'),
    (POINT['latitude']+.003, 10, 'outside'),
    (POINT['latitude']+.0018, 30, 'uncertain'),
    (POINT['latitude'], None, 'uncertain'),
    (POINT['latitude'], 0, 'uncertain'),
    (POINT['latitude'], 500, 'uncertain'),
])
def test_zone_respects_reported_uncertainty(lat, accuracy, state):
    assert classify(POINT, lat, POINT['longitude'], accuracy)['state'] == state


@pytest.mark.parametrize('change', [{'radiusM': True}, {'radiusM': 10}, {'radiusM': 1001},
    {'latitude': float('nan')}, {'longitude': 181}, {'address': 'bad\naddress'}])
def test_invalid_zone_cannot_be_saved(change):
    with pytest.raises(AccessError):
        point_payload(POINT | change)


def test_seed_binds_only_movenpick_and_never_overwrites_edited_zone():
    async def run():
        engine, factory, _work = await setup()
        try:
            async with factory() as session:
                (await session.get(Hotel, 1)).name = 'Miracleon Mövenpick'
                session.add_all([Hotel(id=2, name='Другой отель'), User(id=3, tg_id=3, name='Owner'),
                    UserRole(user_id=3, role='OWNER')])
                await session.commit()
            seed = {'requestId': 'movenpick-location-1', 'match': 'movenpick', 'point': POINT}
            assert (await startup_hotel_point(engine, json.dumps(seed), [3]))['hotelId'] == 1
            async with factory() as session:
                assert (await session.get(Hotel, 1)).address == POINT['address']
                assert not await session.get(Setting, point_key(2))
                (await session.get(Setting, point_key(1))).value = json.dumps(POINT | {'radiusM': 150})
                await session.commit()
            await startup_hotel_point(engine, json.dumps(seed), [3])
            async with factory() as session:
                assert json.loads((await session.get(Setting, point_key(1))).value)['radiusM'] == 150
            with pytest.raises(RuntimeError, match='reused'):
                await startup_hotel_point(engine, json.dumps(seed | {'point': POINT | {'radiusM': 300}}), [3])
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_seed_refuses_ambiguous_hotels():
    async def run():
        engine, factory, _work = await setup()
        try:
            async with factory() as session:
                (await session.get(Hotel, 1)).name = 'Movenpick'
                session.add(Hotel(id=2, name='Мёвенпик'))
                await session.commit()
            result = await startup_hotel_point(engine, json.dumps({'requestId': 'one', 'match': 'movenpick', 'point': POINT}), [3])
            assert result == {'bound': False}
            async with factory() as session:
                assert await session.scalar(select(func.count(Setting.key))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_online_photo_review_and_automatic_time_fine_are_independent(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')
    async def run():
        engine, factory, work = await setup()
        api = work.api
        api.bot = SimpleNamespace()
        api.audit_write = AsyncMock()
        api.body = AsyncMock(side_effect=lambda r: r['body'])
        attendance = Attendance(api)
        attendance.now = lambda: NOW
        attendance.send_photo = AsyncMock(return_value='app:/private/test.jpg')
        try:
            async with factory() as session:
                session.add_all([User(id=3, tg_id=3, name='Owner'), UserRole(user_id=3, role='OWNER'),
                    User(id=4, tg_id=4, name='Other admin'), UserRole(user_id=4, role='ADMIN'),
                    HotelEmployee(user_id=1, hotel_id=1),
                    Setting(key=point_key(1), value=json.dumps(POINT))])
                await session.commit()
            request = {'miniapp_actor': ACTOR}
            attendance.payload = AsyncMock(return_value={'purpose': 'start', 'date': '2026-09-22',
                'latitude': POINT['latitude']+.003, 'longitude': POINT['longitude'], 'accuracy': 10})
            location = json.loads((await attendance.location(request)).text)
            assert location['start']['geofence']['state'] == 'outside'
            attendance.payload.return_value = {'purpose': 'start', 'date': '2026-09-22',
                'image': 'data:image/jpeg;base64,'+base64.b64encode(jpeg()).decode()}
            saved = json.loads((await attendance.photo(request)).text)
            assert saved['start']['status'] == 'PENDING_REVIEW'
            assert saved['start']['at'] is None and saved['fine'] == 500
            async with factory() as session:
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                assert (await session.scalar(select(PayrollEntry))).amount == -500
                row = await session.scalar(select(ShiftCheckIn))
                assert row.started_at is None and row.full_body_file_id == 'app:/private/test.jpg'
            # A pending photo cannot be overwritten by retrying a different one.
            with pytest.raises(AccessError):
                await attendance.photo(request)
            attendance.payload.return_value = {'purpose': 'start', 'date': '2026-09-22',
                'latitude': POINT['latitude'], 'longitude': POINT['longitude'], 'accuracy': 10}
            with pytest.raises(AccessError):
                await attendance.location(request)
            other = json.loads((await attendance.pending({'miniapp_actor': {'id': 4, 'roles': ['ADMIN']}})).text)
            assert other['items'] == []
            pending = json.loads((await attendance.pending({'miniapp_actor': {'id': 3, 'roles': ['OWNER']}})).text)
            assert pending['items'][0]['geofence']['state'] == 'outside'
            review = {'miniapp_actor': {'id': 3, 'roles': ['OWNER']}, 'body': {'userId': 1, 'date': '2026-09-22',
                'purpose': 'start', 'approved': True, 'verifiedAt': '2026-09-22T08:55:00+03:00', 'note': 'Присутствие и форма проверены руководителем'}}
            with pytest.raises(AccessError, match='Свою отметку'):
                await attendance.review(review | {'body': review['body'] | {'userId': 3}})
            await attendance.review(review)
            assert json.loads((await attendance.review(review)).text)['alreadyReviewed']
            async with factory() as session:
                assert (await session.scalar(select(ShiftCheckIn))).status == 'STARTED'
                assert (await session.scalar(select(PayrollEntry))).amount == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_zone_editor_rechecks_scope_and_prevents_stale_updates(monkeypatch):
    async def run():
        engine, factory, work = await setup()
        api = work.api
        api.body = AsyncMock(side_effect=lambda r: r['body'])
        points = HotelPoints(api)
        try:
            async with factory() as session:
                session.add_all([User(id=3, tg_id=3, name='Owner'), UserRole(user_id=3, role='OWNER'),
                    User(id=4, tg_id=4, name='Scoped admin'), UserRole(user_id=4, role='ADMIN'),
                    Hotel(id=2, name='Other'), HotelEmployee(user_id=4, hotel_id=2)])
                await session.commit()
            body = {'hotelId': 1, 'revision': revision(None), 'point': POINT}
            owner = {'miniapp_actor': {'id': 3, 'roles': ['OWNER']}, 'body': body}
            with pytest.raises(AccessError):
                await points.save({'miniapp_actor': {'id': 4, 'roles': ['ADMIN']}, 'body': body})
            await points.save(owner)
            with pytest.raises(AccessError) as error:
                await points.save(owner)
            assert error.value.status == 409
            listed = json.loads((await points.listing({'miniapp_actor': {'id': 4, 'roles': ['ADMIN']}})).text)
            assert [h['id'] for h in listed['items']] == [2]
            async with factory() as session:
                (await session.get(User, 3)).active = False
                await session.commit()
            with pytest.raises(AccessError):
                await points.save(owner)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_context_does_not_choose_nearest_hotel_when_membership_is_ambiguous():
    async def run():
        engine, factory, work = await setup()
        try:
            async with factory() as session:
                session.add_all([Hotel(id=2, name='Other'), HotelEmployee(user_id=1, hotel_id=1),
                    HotelEmployee(user_id=1, hotel_id=2), Setting(key=point_key(1), value=json.dumps(POINT))])
                await session.commit()
            async with engine.begin() as conn:
                result = await capture_context(work.api, conn, 1, date(2026, 9, 22), 'start', 1,
                    POINT['latitude'], POINT['longitude'], 10)
                assert result['state'] == 'ambiguous'
                assert 'hotelId' not in result
                assert await read_json(work.api, conn, context_key('start', 1)) == result
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_protected_mode_validates_real_jpeg_and_blocks_bot_photo_bypass(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')
    with pytest.raises(AccessError):
        photo_bytes({'purpose': 'start', 'date': '2026-09-22',
            'image': 'data:image/jpeg;base64,'+base64.b64encode(b'\xff\xd8\xff'+b'fake'*50+b'\xff\xd9').decode()})
    assert photo_bytes({'purpose': 'start', 'date': '2026-09-22',
        'image': 'data:image/jpeg;base64,'+base64.b64encode(jpeg()).decode()}) == jpeg()
    async def run():
        from app.handlers.photographer import save_shift_photo, save_workplace_photo
        for handler in [save_shift_photo, save_workplace_photo]:
            message = SimpleNamespace(answer=AsyncMock())
            state = SimpleNamespace(clear=AsyncMock())
            await handler(message, state, ['PHOTOGRAPHER'])
            assert 'Photo Boss' in message.answer.call_args.args[0]
            state.clear.assert_awaited_once()
    asyncio.run(run())
