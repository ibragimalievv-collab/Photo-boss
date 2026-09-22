import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_workflow_replay import ACTOR, packet, setup

from app.attendance import Attendance
from app.miniapp_security import AccessError
from app.models import PayrollEntry, ShiftCheckIn, ShiftCheckOut, User, UserRole

NOW = datetime(2026, 9, 22, 18, tzinfo=timezone.utc)
RAW = b'\xff\xd8\xff' + b'camera' * 60 + b'\xff\xd9'


async def prepare(monkeypatch):
    engine, factory, work = await setup()
    work.api.audit_write = AsyncMock()
    work.api.body = AsyncMock(side_effect=lambda r: r['body'])
    work.api.bot = SimpleNamespace()
    monkeypatch.setattr(Attendance, 'now', lambda _: NOW)
    monkeypatch.setattr(Attendance, 'send_photo', AsyncMock(return_value='fresh-camera'))
    async with factory() as session:
        session.add_all([User(id=3, tg_id=3, name='Owner'), UserRole(user_id=3, role='OWNER')])
        await session.commit()
    return engine, factory, work, Attendance(work.api)


def operation(purpose, when):
    return packet('attendance-' + purpose, 'attendance', {'purpose': purpose, 'claimedAt': when,
        'latitude': 41.5, 'longitude': 48.1, 'accuracy': 10})


def decision(purpose, when, approved=True, actor=None):
    return {'miniapp_actor': actor or {'id': 3, 'roles': ['OWNER']}, 'body': {
        'userId': 1, 'date': '2026-09-22', 'purpose': purpose, 'approved': approved,
        'verifiedAt': when, 'note': 'Проверены фото, график и обстоятельства потери связи'}}


def test_offline_review_does_not_fine_delivery_delay_and_preserves_single_ledger(monkeypatch):
    async def run():
        engine, factory, work, attendance = await prepare(monkeypatch)
        try:
            for purpose, when in [('start', '2026-09-22T08:55:00+03:00'), ('end', '2026-09-22T18:00:00+03:00')]:
                body = operation(purpose, when)
                first = json.loads((await work.execute(ACTOR, body, RAW)).text)
                assert first == json.loads((await work.execute(ACTOR, body, RAW)).text)
                assert first['attendanceStatus'] == 'PENDING_REVIEW'
            async with factory() as session:
                assert (await session.scalar(select(ShiftCheckIn))).started_at is None
                assert (await session.scalar(select(ShiftCheckOut))).ended_at is None
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
            with pytest.raises(AccessError, match='Сначала подтвердите начало'):
                await attendance.review(decision('end', '2026-09-22T18:00:00+03:00'))
            await attendance.review(decision('start', '2026-09-22T08:55:00+03:00'))
            await attendance.review(decision('end', '2026-09-22T18:00:00+03:00'))
            assert json.loads((await attendance.review(decision('start', '2026-09-22T08:55:00+03:00'))).text)['alreadyReviewed']
            async with factory() as session:
                start = await session.scalar(select(ShiftCheckIn))
                assert start.status == 'STARTED' and not start.late and start.offline_claimed_at is not None
                assert await session.scalar(select(func.count(ShiftCheckIn.id))) == 1
                assert (await session.scalar(select(ShiftCheckOut))).status == 'FINISHED'
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_review_rechecks_roles_time_and_charges_only_once(monkeypatch):
    async def run():
        engine, factory, work, attendance = await prepare(monkeypatch)
        try:
            body = operation('start', '2026-09-22T08:55:00+03:00')
            await work.execute(ACTOR, body, RAW)
            with pytest.raises(AccessError):
                await attendance.review(decision('start', '2026-09-22T08:55:00+03:00', actor=ACTOR))
            for invalid in ['2026-09-22T08:55:00', '2026-09-23T08:55:00+03:00']:
                with pytest.raises(AccessError):
                    await attendance.review(decision('start', invalid))
            reviewed = decision('start', '2026-09-22T09:15:00+03:00')
            await attendance.review(reviewed)
            await attendance.review(reviewed)
            await work.execute(ACTOR, body, RAW)
            async with factory() as session:
                fees = (await session.scalars(select(PayrollEntry))).all()
                assert len(fees) == 1 and fees[0].amount == -500 and fees[0].user_id == 1
                start = await session.scalar(select(ShiftCheckIn))
                assert start.late and start.fine_amount == 500
                assert start.started_at != start.offline_claimed_at
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_invalid_capture_rejected_and_rejection_preserves_evidence(monkeypatch):
    async def run():
        engine, factory, work, attendance = await prepare(monkeypatch)
        try:
            for when in ['2026-09-22T08:55:00', '2026-09-21T08:55:00+03:00', '2026-09-22T23:55:00+03:00']:
                with pytest.raises(AccessError):
                    await work.execute(ACTOR, operation('start', when), RAW)
            with pytest.raises(AccessError):
                await work.execute(ACTOR, operation('start', '2026-09-22T08:55:00+03:00'), b'fake')
            await work.execute(ACTOR, operation('start', '2026-09-22T08:55:00+03:00'), RAW)
            await attendance.review(decision('start', None, approved=False))
            await attendance.review(decision('start', None, approved=False))
            async with factory() as session:
                row = await session.scalar(select(ShiftCheckIn))
                assert row.status == 'REJECTED' and row.full_body_file_id == 'fresh-camera'
                assert row.started_at is None and row.offline_claimed_at is not None
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())
