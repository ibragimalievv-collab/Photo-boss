"""Photographer check-in must fail closed and use the selected schedule."""
import asyncio
import base64
import io
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import func, select
from test_workflow_replay import setup

from app.attendance import Attendance
from app.miniapp_security import AccessError
from app.models import Hotel, PayrollEntry, Shift, ShiftCheckIn
from app.services.attendance_ai import analyze_checkin, validated_result

NOW = datetime(2026, 9, 22, 10, tzinfo=timezone.utc)
ACTOR = {'id': 1, 'roles': ['MANAGER'], 'tg_id': 1}


def jpeg(solid=False):
    photo = Image.new('RGB', (300, 500), 'white')
    if not solid:
        draw = ImageDraw.Draw(photo)
        draw.ellipse((110, 20, 190, 100), fill='black')
        draw.rectangle((95, 100, 205, 280), fill='blue')
        draw.rectangle((95, 280, 140, 460), fill='black')
        draw.rectangle((160, 280, 205, 460), fill='black')
    buffer = io.BytesIO()
    photo.save(buffer, format='JPEG')
    return buffer.getvalue()


def verdict(**changes):
    value = {'person_count': 1, 'head_visible': True, 'both_feet_visible': True,
             'whole_body_visible': True, 'usable_photo': True, 'confidence': .95, 'note': 'Человек виден в полный рост.'}
    return value | changes


@pytest.mark.parametrize('changes', [
    {'person_count': 0}, {'person_count': 2}, {'head_visible': False},
    {'both_feet_visible': False}, {'whole_body_visible': False},
    {'usable_photo': False}, {'confidence': .84},
])
def test_full_body_requires_every_visible_criterion(changes):
    assert validated_result(verdict(**changes))['status'] == 'REJECTED'
    assert validated_result(verdict())['status'] == 'ACCEPTED'


@pytest.mark.parametrize('changes', [{'confidence': float('nan')}, {'confidence': True}, {'person_count': True}, {'head_visible': 1}, {'note': ''}])
def test_invalid_ai_payload_never_accepts(changes):
    with pytest.raises(ValueError):
        validated_result(verdict(**changes))


def test_solid_image_rejected_without_paid_request():
    assert asyncio.run(analyze_checkin(jpeg(solid=True)))['status'] == 'REJECTED'
    assert asyncio.run(analyze_checkin(b'not jpeg'))['status'] == 'REJECTED'


async def prepare(monkeypatch):
    engine, factory, work = await setup()
    work.api.audit_write = AsyncMock()
    work.api.bot = SimpleNamespace(send_photo=AsyncMock(side_effect=AssertionError('Check-in must not depend on Telegram')))
    service = Attendance(work.api)
    monkeypatch.setattr(service, 'now', lambda: NOW)
    async with factory() as session:
        session.add(Hotel(id=2, name='Second hotel'))
        session.add_all([
            Shift(id=1, user_id=1, hotel_id=1, start_at=NOW.replace(tzinfo=None) + timedelta(hours=1), end_at=NOW.replace(tzinfo=None) + timedelta(hours=3)),
            Shift(id=2, user_id=1, hotel_id=2, start_at=NOW.replace(tzinfo=None) + timedelta(hours=4), end_at=NOW.replace(tzinfo=None) + timedelta(hours=7)),
        ])
        await session.commit()
    return engine, factory, service


async def save_location(service, monkeypatch, sid=1):
    monkeypatch.setattr(service, 'payload', AsyncMock(return_value={'purpose': 'start', 'date': '2026-09-22', 'latitude': 41.5, 'longitude': 48.1, 'accuracy': 10, 'shiftId': sid}))
    return await service.location({'miniapp_actor': ACTOR})


async def save_photo(service, monkeypatch, status='ACCEPTED', sid=1):
    monkeypatch.setattr(service, 'payload', AsyncMock(return_value={'purpose': 'start', 'date': '2026-09-22', 'image': 'data:image/jpeg;base64,' + base64.b64encode(jpeg()).decode(), 'shiftId': sid}))
    analysis = AsyncMock(return_value={'status': status, 'note': 'Результат проверки.'})
    monkeypatch.setattr('app.attendance.analyze_checkin', analysis)
    response = await service.photo({'miniapp_actor': ACTOR})
    return json.loads(response.text), analysis


def test_failed_ai_keeps_retryable_evidence_without_start_or_fine_then_accepts_once(monkeypatch):
    async def run():
        engine, factory, service = await prepare(monkeypatch)
        try:
            await save_location(service, monkeypatch)
            for status in ('REJECTED', 'UNAVAILABLE'):
                result, _ = await save_photo(service, monkeypatch, status)
                assert result['start']['status'] == 'AWAITING_PHOTO'
                assert result['start']['aiStatus'] == status
                assert result['fine'] == 0
                async with factory() as session:
                    row = await session.scalar(select(ShiftCheckIn))
                    assert row.started_at is None and row.photo_data
                    assert row.full_body_file_id.startswith('attendance-db:')
                    assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
            accepted, _ = await save_photo(service, monkeypatch)
            assert accepted['start']['status'] == 'STARTED'
            assert accepted['shiftId'] == 1 and accepted['startTime'] == '14:00'
            assert not accepted['late']  # 13:00 is on time for this 14:00 plan.
            replayed, analysis = await save_photo(service, monkeypatch)
            assert replayed['start']['status'] == 'STARTED'
            analysis.assert_not_awaited()
            async with factory() as session:
                assert await session.scalar(select(func.count(ShiftCheckIn.id))) == 1
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
            service.api.bot.send_photo.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_location_expiry_prevents_ai_call_and_wrong_shift_is_rejected(monkeypatch):
    async def run():
        engine, factory, service = await prepare(monkeypatch)
        try:
            with pytest.raises(AccessError, match='Смена недоступна'):
                await save_location(service, monkeypatch, sid=999)
            await save_location(service, monkeypatch)
            monkeypatch.setattr(service, 'now', lambda: NOW + timedelta(minutes=6))
            with pytest.raises(AccessError, match='Геолокация устарела'):
                await save_photo(service, monkeypatch)
            async with factory() as session:
                assert (await session.scalar(select(ShiftCheckIn))).started_at is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_legacy_recapture_preserves_prior_start_and_never_charges_again(monkeypatch):
    async def run():
        engine, factory, service = await prepare(monkeypatch)
        try:
            original = NOW.replace(tzinfo=None) - timedelta(minutes=30)
            async with factory() as session:
                session.add(ShiftCheckIn(user_id=1, shift_date=date(2026,9,22), scheduled_shift_id=1,
                    status='STARTED', started_at=original, late=True, fine_amount=500))
                session.add(PayrollEntry(user_id=1, kind='Штраф за опоздание', amount=-500, period='2026-09-22'))
                await session.commit()
            await save_location(service, monkeypatch)
            result, _ = await save_photo(service, monkeypatch)
            assert result['start']['status'] == 'STARTED'
            async with factory() as session:
                row = await session.scalar(select(ShiftCheckIn))
                assert row.started_at == original and row.ai_status == 'ACCEPTED'
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_hotel_point_and_accuracy_are_enforced_before_location_and_photo_confirmation(monkeypatch):
    async def run():
        engine, factory, service = await prepare(monkeypatch)
        try:
            async with factory() as session:
                hotel = await session.get(Hotel, 1)
                hotel.latitude, hotel.longitude, hotel.attendance_radius_m = 41.5, 48.1, 100
                await session.commit()
            for lat, accuracy in [(41.6, 10), (41.5, None), (41.5, 150)]:
                monkeypatch.setattr(service, 'payload', AsyncMock(return_value={'purpose': 'start', 'date': '2026-09-22',
                    'latitude': lat, 'longitude': 48.1, 'accuracy': accuracy, 'shiftId': 1}))
                with pytest.raises(AccessError):
                    await service.location({'miniapp_actor': ACTOR})
            async with factory() as session:
                assert await session.scalar(select(func.count(ShiftCheckIn.id))) == 0
            await save_location(service, monkeypatch)
            async with factory() as session:
                assert (await session.scalar(select(ShiftCheckIn))).location_accuracy == 10
                # A changed expected point invalidates even fresh saved coordinates.
                hotel = await session.get(Hotel, 1)
                hotel.latitude = 41.6
                await session.commit()
            with pytest.raises(AccessError):
                await save_photo(service, monkeypatch)
            async with factory() as session:
                assert (await session.scalar(select(ShiftCheckIn))).started_at is None
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_unscoped_checkin_cannot_bypass_configured_hotel_point(monkeypatch):
    from app.services import shifts
    monkeypatch.setattr(shifts, 'shift_now', lambda value=None: NOW)

    async def run():
        engine, factory, work = await setup()
        try:
            async with factory() as session:
                session.add(ShiftCheckIn(user_id=1, shift_date=date(2026,9,22), status='STARTED',
                    started_at=NOW.replace(tzinfo=None), ai_status='ACCEPTED'))
                await session.commit()
                assert (await shifts.require_active_shift(session, 1, 1)).user_id == 1
                assert (await shifts.require_active_shift_conn(work.api, session, 1, 1))['user_id'] == 1
                hotel = await session.get(Hotel, 1)
                hotel.latitude, hotel.longitude, hotel.attendance_radius_m = 41.5, 48.1, 100
                await session.commit()
                with pytest.raises(ValueError, match='назначить смену в графике'):
                    await shifts.require_active_shift(session, 1, 1)
                with pytest.raises(ValueError, match='назначить смену в графике'):
                    await shifts.require_active_shift_conn(work.api, session, 1, 1)
        finally:
            await engine.dispose()
    asyncio.run(run())
