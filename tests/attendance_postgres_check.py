"""Shift cycle and notification lock regression in a disposable CI schema."""
import asyncio
import json
import os
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import test_workflow_services
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_attendance_auto_confirmation import DAY, NOW, prepare, submit
from test_workflow_replay import ACTOR, packet

from app.db import user_lock_clause
from app.models import Notification, ShiftCheckIn, ShiftCheckOut, WorkBreak
from app.push_notifications import queue_notice
from app.work_breaks import WorkBreaks


async def main():
    # Never defaults to DATABASE_URL: this script must not touch production.
    url = os.environ['UPDATE_POSTGRES_TEST_URL']
    schema = 'pb_attendance_test_' + uuid4().hex
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        with patch.object(test_workflow_services, 'create_async_engine', return_value=engine), patch.dict(os.environ, {
                'PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED': '1',
                'PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED': '0',
                'PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW': '1'}):
            _, factory, work, service = await prepare()
            async def rows(conn, sql, **params):
                return list((await conn.execute(text(sql), params)).mappings())
            work.api.rows = rows
            state = await submit(service, latitude=44.9)
            assert state['start']['status'] == 'STARTED'
            await service.photo({'miniapp_actor': ACTOR})
            clock = NOW.replace(tzinfo=None) + timedelta(hours=3)
            with patch('app.work_breaks.utc_now', side_effect=lambda: clock):
                start_packet = packet('pg-lunch-start', 'break_start', {'hotelId': 1})
                first = json.loads((await work.execute(ACTOR, start_packet)).text)
                assert first == json.loads((await work.execute(ACTOR, start_packet)).text)
                clock += timedelta(minutes=31)
                end_packet = packet('pg-lunch-return', 'break_end', {'hotelId': 1})
                await work.execute(ACTOR, end_packet)
                await work.execute(ACTOR, end_packet)
                lunch = json.loads((await WorkBreaks(work.api).listing({'miniapp_actor': ACTOR})).text)
                assert len(lunch['items']) == 1 and lunch['items'][0]['minutes'] == 31
            service.now = lambda: NOW + timedelta(hours=9)
            assert (await submit(service, 'end', latitude=44.9))['end']['status'] == 'FINISHED'
            await service.photo({'miniapp_actor': ACTOR})
            owner = {'id': 3, 'roles': ['OWNER']}
            listing = json.loads((await service.pending({'miniapp_actor': owner})).text)
            assert len(listing['items']) == 2
            assert all(item['confirmedAt'] and 'confirmed_at' not in item for item in listing['items'])
            first = next(item for item in listing['items'] if item['purpose'] == 'start')
            correction = {'miniapp_actor': owner, 'body': {'userId': 1, 'date': str(DAY),
                'purpose': 'start', 'approved': True, 'expectedAt': first['confirmedAt'],
                'verifiedAt': '2026-09-22T09:10:00+03:00', 'note': 'Isolated CI leadership review'}}
            await service.review(correction)
            assert json.loads((await service.review(correction)).text)['alreadyReviewed']
            async with factory() as session:
                for model in (ShiftCheckIn, ShiftCheckOut, WorkBreak):
                    assert await session.scalar(select(func.count(model.id))) == 1
                saved = await session.scalar(select(WorkBreak))
                assert saved.ended_at - saved.started_at == timedelta(minutes=31)
            print('PASS PostgreSQL: fresh GPS/photo, start, 31-minute lunch/return, end, leadership list/review, exact replays')

            # Both operations hold their own user lock before inserting notices
            # for each other. FOR UPDATE creates the production FK lock cycle.
            ready = [asyncio.Event(), asyncio.Event()]
            async def notifying(index, worker, recipient):
                async with engine.begin() as conn:
                    await conn.execute(text('SELECT id FROM users WHERE id=:id' + user_lock_clause(conn)), {'id': worker})
                    ready[index].set()
                    await ready[1-index].wait()
                    async with AsyncSession(bind=conn) as session:
                        await queue_notice(session, recipient, kind='attendance',
                            event_key=f'pg-cross-lock:{worker}', message='CI only')
            await asyncio.wait_for(asyncio.gather(notifying(0, 1, 3), notifying(1, 3, 1)), timeout=10)
            async with factory() as session:
                assert await session.scalar(select(func.count(Notification.id)).where(
                    Notification.event_key.like('pg-cross-lock:%'))) == 2
            print('PASS PostgreSQL: simultaneous reciprocal notifications complete without FK deadlock')
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
