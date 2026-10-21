"""Review decisions preserve one attendance/fine ledger and notify its owner."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select
from test_attendance_auto_confirmation import DAY, NOW, prepare, submit
from test_hotel_points_review import jpeg
from test_workflow_replay import ACTOR, packet

from app.miniapp_security import AccessError
from app.models import Notification, PayrollEntry, Shift, ShiftCheckIn, ShiftCheckOut

OWNER = {'id': 3, 'roles': ['OWNER']}


def decision(*, approved=True, verified_at='2026-09-22T09:10:00+03:00', **extra):
    return {'miniapp_actor': OWNER, 'body': {'userId': 1, 'date': str(DAY),
        'purpose': 'start', 'approved': approved, 'verifiedAt': verified_at,
        'note': 'Руководитель проверил сохранённые сведения', **extra}}


async def review_notices(factory):
    async with factory() as session:
        rows = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance')))
        return [row for row in rows if json.loads(row.payload).get('reviewed')]


def test_approval_and_each_correction_notify_worker_and_scoped_leaders_once(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            await submit(service, latitude=0)
            request = decision()
            await service.review(request)
            await service.review(request)
            notices = await review_notices(factory)
            assert {row.user_id for row in notices} == {1, 3, 4}
            assert len(notices) == 3
            assert all('подтверждена руководителем' in row.text for row in notices)
            assert all(json.loads(row.payload)['status'] == 'STARTED' for row in notices)
            correction = decision(verified_at='2026-09-22T08:55:00+03:00',
                expectedAt='2026-09-22T06:10:00+00:00')
            await service.review(correction)
            await service.review(correction)
            notices = await review_notices(factory)
            assert len(notices) == 6
            assert sum('исправлено руководителем' in row.text for row in notices) == 3
            # Returning to an earlier time is a new correction, not an old
            # notification replay; exact HTTP retries still create nothing.
            restored = decision(expectedAt='2026-09-22T05:55:00+00:00')
            await service.review(restored)
            await service.review(restored)
            assert len(await review_notices(factory)) == 9
            async with factory() as session:
                active = list(await session.scalars(select(Notification).where(
                    Notification.kind == 'attendance', Notification.resolved_at.is_(None))))
                assert len(active) == 3
                assert all('09:10' in row.text for row in active)
                assert all(json.loads(row.payload).get('reviewed') for row in active)
                assert await session.scalar(select(func.count(ShiftCheckIn.id))) == 1
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                assert (await session.scalar(select(PayrollEntry))).amount == -500
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_rejection_reconciles_unplanned_charge_immediately_and_notifies_once(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            async with factory() as session:
                await session.execute(delete(Shift))
                await session.commit()
            await submit(service, latitude=0)
            async with factory() as session:
                assert (await session.scalar(select(PayrollEntry))).amount == -500
            request = decision(approved=False, verified_at=None)
            await service.review(request)
            await service.review(request)
            async with factory() as session:
                row = await session.scalar(select(ShiftCheckIn))
                assert row.status == 'REJECTED' and not row.late and row.fine_amount == 0
                assert row.full_body_file_id and row.offline_claimed_at
                assert (await session.scalar(select(PayrollEntry))).amount == 0
            notices = await review_notices(factory)
            assert {row.user_id for row in notices} == {1, 3}
            assert len(notices) == 2
            assert all('отклонена руководителем' in row.text for row in notices)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_incomplete_pending_start_cannot_authorize_offline_end_or_approval(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, work, service = await prepare()
        try:
            async with factory() as session:
                session.add(ShiftCheckIn(user_id=1, shift_date=DAY,
                    status='PENDING_REVIEW', offline_claimed_at=NOW.replace(tzinfo=None)))
                await session.commit()
            pending = json.loads((await service.pending({'miniapp_actor': OWNER})).text)
            assert len(pending['items']) == 1
            assert pending['items'][0]['location_received_at'] is None
            service.send_photo = AsyncMock()
            with pytest.raises(AccessError, match='Сначала синхронизируйте начало'):
                async with work.api.engine.begin() as conn:
                    await service.offline(conn, ACTOR, DAY, packet('end-incomplete-start', 'attendance',
                        {'purpose': 'end', 'claimedAt': NOW.isoformat(),
                         'latitude': 44.9, 'longitude': 37.3, 'accuracy': 10})['data'], jpeg())
            with pytest.raises(AccessError, match='не сохранены фото и геолокация'):
                await service.review(decision())
            service.send_photo.assert_not_awaited()
            async with factory() as session:
                assert await session.scalar(select(func.count(ShiftCheckOut.id))) == 0
                assert (await session.scalar(select(ShiftCheckIn))).status == 'PENDING_REVIEW'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_legacy_incomplete_pending_can_be_rejected_without_inventing_time(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            async with factory() as session:
                session.add(ShiftCheckIn(user_id=1, shift_date=DAY, status='PENDING_REVIEW'))
                await session.commit()
            pending = json.loads((await service.pending({'miniapp_actor': OWNER})).text)
            assert pending['items'][0]['offline_claimed_at'] is None
            request = decision(approved=False, verified_at=None)
            await service.review(request)
            await service.review(request)
            notices = await review_notices(factory)
            assert len(notices) == 3
            assert all('время не сохранено' in row.text for row in notices)
            assert all(json.loads(row.payload)['status'] == 'REJECTED' for row in notices)
            async with factory() as session:
                row = await session.scalar(select(ShiftCheckIn))
                assert row.status == 'REJECTED' and row.started_at is None
                assert row.offline_claimed_at is None and row.location_received_at is None
        finally:
            await engine.dispose()
    asyncio.run(run())
