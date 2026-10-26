"""Lunch changes and missing-return anomalies reach only current supervisors."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_attendance_auto_confirmation import DAY, NOW, prepare
from test_team import Request

from app.miniapp_security import AccessError
from app.models import HotelEmployee, Notification, User, WorkBreak
from app.push_notifications import AppPush, payload_for
from app.work_breaks import WorkBreaks, close_breaks, stamp


def test_manual_lunch_creation_and_correction_notifies_owner_scoped_admin_and_worker(monkeypatch):
    monkeypatch.setattr('app.work_breaks.utc_now', lambda: NOW.replace(tzinfo=None))

    async def run():
        engine, factory, work, _attendance = await prepare()
        work.api.body = AsyncMock(side_effect=lambda request: request.body)
        service = WorkBreaks(work.api)
        push = AppPush(SimpleNamespace(engine=engine))
        body = {'userId': 1, 'hotelId': 1, 'date': str(DAY),
                'startedAt': (NOW - timedelta(minutes=40)).isoformat(),
                'endedAt': (NOW - timedelta(minutes=10)).isoformat(),
                'reason': 'Время подтверждено руководителем по журналу'}
        try:
            result = json.loads((await service.save(Request({'id': 4, 'roles': ['ADMIN']}, body))).text)
            async with factory() as session:
                row = await session.get(WorkBreak, result['id'])
                original_revision = stamp(row.updated_at)
                notices = list(await session.scalars(select(Notification)))
                assert len(notices) == 3 and {notice.user_id for notice in notices} == {1, 3, 4}
                assert all(notice.kind == 'lunch_corrected' for notice in notices)
                assert all('добавил отметку' in notice.text for notice in notices)
            correction = {'startedAt': (NOW - timedelta(minutes=35)).isoformat(),
                          'endedAt': (NOW - timedelta(minutes=5)).isoformat(),
                          'reason': 'Уточнили начало и возвращение', 'revision': original_revision}
            await service.save(Request({'id': 3, 'roles': ['OWNER']}, correction, result['id']))
            # Even a frozen clock must advance the revision and reject exact
            # repeated edits, so no second correction notice is generated.
            with pytest.raises(AccessError, match='уже изменена'):
                await service.save(Request({'id': 3, 'roles': ['OWNER']}, correction, result['id']))
            async with factory() as session:
                notices = list(await session.scalars(select(Notification)))
                assert len(notices) == 6
                latest = [notice for notice in notices if not json.loads(notice.payload)['created']]
                assert len(latest) == 3 and all('Уточнили начало и возвращение' in n.text for n in latest)
                for notice in notices:
                    visible = payload_for(notice)
                    assert visible['url'] == '/app/#workday'
                    assert 'Manager' not in json.dumps(visible) and body['reason'] not in json.dumps(visible)
            assert len(json.loads((await push.status(Request({'id': 4}))).text)['items']) == 2
            async with factory() as session:
                await session.delete(await session.scalar(select(HotelEmployee).where(HotelEmployee.user_id == 4)))
                await session.commit()
            assert json.loads((await push.status(Request({'id': 4}))).text)['items'] == []
            assert json.loads((await push.status(Request({'id': 5}))).text)['items'] == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_shift_end_without_return_queues_atomic_review_notice_once(monkeypatch):
    monkeypatch.setattr('app.work_breaks.utc_now', lambda: NOW.replace(tzinfo=None))

    async def run():
        engine, factory, _work, _attendance = await prepare()
        try:
            async with factory() as session:
                row = WorkBreak(user_id=1, hotel_id=1, shift_date=DAY,
                                started_at=NOW.replace(tzinfo=None) - timedelta(minutes=30))
                session.add(row)
                await session.commit()
                bid = row.id
            async with factory() as session:
                await close_breaks(session, 1, DAY, NOW.replace(tzinfo=None), await session.get(User, 1))
                assert await session.scalar(select(func.count(Notification.id))) == 3
                await session.rollback()
            async with factory() as session:
                assert (await session.get(WorkBreak, bid)).ended_at is None
                assert await session.scalar(select(func.count(Notification.id))) == 0
                actor = await session.get(User, 1)
                await close_breaks(session, 1, DAY, NOW.replace(tzinfo=None), actor)
                await close_breaks(session, 1, DAY, NOW.replace(tzinfo=None), actor)
                await session.commit()
            async with factory() as session:
                assert (await session.get(WorkBreak, bid)).ended_at == NOW.replace(tzinfo=None)
                notices = list(await session.scalars(select(Notification)))
                assert len(notices) == 3 and {notice.user_id for notice in notices} == {1, 3, 4}
                assert all(notice.kind == 'lunch_attention' for notice in notices)
                assert all('Отдельной отметки возвращения нет' in notice.text for notice in notices)
                assert all(json.loads(notice.payload)['automatic'] for notice in notices)
                assert all(payload_for(notice)['title'] == 'Photo Boss · Нужно проверить отметку обеда' for notice in notices)
        finally:
            await engine.dispose()

    asyncio.run(run())
