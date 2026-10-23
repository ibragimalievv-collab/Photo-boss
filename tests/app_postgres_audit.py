"""Four-role HTTP read audit against an isolated PostgreSQL schema.

Uses the production route installers and middleware. No production credentials,
provider calls, employee data or background message loops are used.
"""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import test_workflow_services
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_attendance_auto_confirmation import prepare, submit
from test_miniapp_release import TOKEN, signed

from app.academy_practice import install_academy_practice
from app.attendance import install_attendance
from app.miniapp_api import install_miniapp
from app.models import AuditLog, Setting, utc_now
from app.owner_telegram import START_KEY, queue_owner_work_events
from app.people import install_people
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.work_calls import install_work_calls
from app.work_chat import install_work_chat
from app.yandex_disk import install_yandex_disk

COMMON = ('me', 'dashboard', 'bookings?date=2026-09-22', 'schedule?week=2026-09-22',
          'finance', 'academy', 'academy/coach', 'academy/mentor', 'academy/practice',
          'academy/development', 'workflow', 'workday', 'work-breaks', 'onboarding',
          'galleries', 'booking-control', 'attendance', 'push', 'chat/people',
          'chat/presence', 'chat/unread', 'chat/calls', 'chat/calls/history')
LEADERS = ('attendance/pending', 'attendance/fines', 'hotel-points', 'people',
           'documents', 'hr', 'discipline?from=2026-09-22&to=2026-09-22', 'shoot-control')
OWNER = ('delivery-contacts', 'audit', 'insights', 'events', 'storage/status', 'storage/inventory',
         'storage/sales-audit', 'diagnostics/ai')


async def main():
    url = os.environ['UPDATE_POSTGRES_TEST_URL']
    schema = 'pb_app_audit_' + uuid4().hex
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    client = None
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        with patch.object(test_workflow_services, 'create_async_engine', return_value=engine), patch.dict(os.environ, {
                'PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED': '1',
                'PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED': '0',
                'PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW': '1',
                'PHOTO_BOSS_NOTIFICATION_CHANNEL': 'app'}):
            _, _, _, attendance = await prepare()
            async def rows(conn, sql, **params):
                return list((await conn.execute(text(sql), params)).mappings())
            attendance.api.rows = rows
            await submit(attendance, latitude=44.9)
            from sqlalchemy.ext.asyncio import AsyncSession
            async with AsyncSession(engine) as session:
                session.add(Setting(key=START_KEY, value=utc_now().isoformat()))
                session.add(AuditLog(action='booking_created', entity='booking', entity_id=123))
                await session.commit()
            await queue_owner_work_events(engine)
            await queue_owner_work_events(engine)
            async with engine.connect() as conn:
                assert await conn.scalar(text("SELECT count(*) FROM notifications WHERE kind='work_event'")) == 1
            bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock(),
                me=AsyncMock(return_value=SimpleNamespace(username='isolated_qa_bot')))
            app = web.Application(client_max_size=25*1024*1024)
            api = install_miniapp(app, engine=engine, bot=bot,
                lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
            install_attendance(app, api)
            install_people(app, api)
            install_academy_practice(app, api)
            install_work_calls(app, install_work_chat(app, api))
            install_yandex_disk(app)
            client = TestClient(TestServer(app))
            await client.start_server()
            failures, checks = [], 0
            for uid, role in ((1, 'manager'), (2, 'photographer'), (3, 'owner'), (4, 'admin')):
                headers = {'X-Telegram-Init-Data': signed(uid)}
                rules = await (await client.get('/api/miniapp/chat/rules', headers=headers)).json()
                accepted = await client.post('/api/miniapp/chat/rules/accept', headers=headers,
                    json={'version': rules['version'], 'sha256': rules['sha256']})
                assert accepted.status == 200, await accepted.text()
                for group in (COMMON, LEADERS, OWNER):
                    allowed = group is COMMON or (group is LEADERS and role in {'owner', 'admin'}) or role == 'owner'
                    for path in group:
                        response = await client.get('/api/miniapp/' + path,
                            headers={'X-Telegram-Init-Data': signed(uid)})
                        body = await response.text()
                        checks += 1
                        # Some financial/control pages intentionally permit staff
                        # self-scope. Regardless of permission, no route may 5xx.
                        if response.status >= 500 or (allowed and response.status != 200):
                            failures.append((role, path, response.status, body[:180]))
                        if response.status == 200:
                            json.loads(body)
                        if role not in {'owner', 'admin'} and path in {'people', 'attendance/pending', 'hr'} and response.status != 403:
                            failures.append((role, path, 'privilege leak', response.status))
                        if role != 'owner' and path in {'audit', 'storage/status', 'diagnostics/ai'} and response.status != 403:
                            failures.append((role, path, 'owner privilege leak', response.status))
            assert bot.send_message.await_count == 0
            assert not failures, json.dumps(failures, ensure_ascii=False, indent=2)
            print(f'PASS: {checks} real HTTP role/JSON checks on PostgreSQL; no external messages')
    finally:
        if client:
            await client.close()
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
