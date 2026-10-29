import asyncio
import json

import pytest
from notification_feed_fixture import NOW, check_large_feed, check_scope, request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.miniapp_security import AccessError
from app.models import Notification, User
from app.push_notifications import AppPush


def test_feed_sql_matches_current_delivery_scope_and_revocation(monkeypatch):
    monkeypatch.setattr('app.push_notifications.current_utc', lambda: NOW)
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        try:
            await check_scope(engine)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_bounded_page_exact_unread_persisted_read_all_and_private_history():
    async def run():
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        try:
            result = await check_large_feed(engine)
            assert result['queries'] <= 13
            api = AppPush(SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda r: r.body)))
            factory = async_sessionmaker(engine)
            async with factory() as session:
                other = await session.scalar(select(Notification.id).where(Notification.user_id == 2))
            with pytest.raises(AccessError) as denied:
                await api.read(request(1, {'id': other}))
            assert denied.value.status == 404
            for query in ({'limit': '0'}, {'limit': '31'}, {'limit': '-1'}, {'before': '-1'}, {'before': 'abc'}):
                with pytest.raises(AccessError) as rejected:
                    await api.status(request(1, query=query))
                assert rejected.value.status == 400
            for body in ({}, {'throughId': True}, {'throughId': 0}, {'throughId': 2**63}, {'throughId': 1, 'userId': 2}):
                with pytest.raises(AccessError) as rejected:
                    await api.read_all(request(1, body))
                assert rejected.value.status == 400
            async with factory() as session:
                (await session.get(User, 1)).active = False
                await session.commit()
            with pytest.raises(AccessError) as denied:
                await api.status(request(1))
            assert denied.value.status == 403
            assert json.loads((await api.status(request(2))).text)['unreadCount'] == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_notification_http_routes_auth_scope_and_persisted_read_all(tmp_path):
    async def run():
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from test_miniapp_release import TOKEN, signed
        from test_workflow_services import fixture

        from app.miniapp_api import install_miniapp
        from app.push_notifications import queue_notice
        # The installed application starts background readers. Give each
        # transaction its own connection, as PostgreSQL does in production.
        engine, factory = await fixture(f'sqlite+aiosqlite:///{tmp_path / "http.sqlite"}')
        bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock())
        app = web.Application()
        install_miniapp(app, engine=engine, bot=bot, lessons=[])
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            async with factory() as session:
                own = await queue_notice(session, 1, kind='chat', event_key='own-http')
                foreign = await queue_notice(session, 2, kind='chat', event_key='foreign-http')
                # A malformed scoped legacy payload must never make the SQL
                # counter fail or grant access to an unrelated booking.
                session.add(Notification(user_id=1, kind='operational_alert', text='Bad payload', payload='bad json'))
                await session.commit()
            async with factory() as session:
                existing = [(n.id, n.user_id, n.kind, n.acknowledged_at, n.resolved_at) for n in await session.scalars(select(Notification))]
                assert len(existing) == 3, existing
            headers = {'X-Telegram-Init-Data': signed(1)}
            assert (await client.get('/api/miniapp/push')).status == 401
            data = await (await client.get('/api/miniapp/push?limit=1', headers=headers)).json()
            assert data['unreadCount'] == 1 and [n['id'] for n in data['items']] == [own], (json.dumps(data), existing)
            assert (await client.post('/api/miniapp/push/read', headers=headers, json={'id': foreign})).status == 404
            read = await client.post('/api/miniapp/push/read-all', headers=headers, json={'throughId': own})
            assert read.status == 200 and (await read.json())['unreadCount'] == 0
            again = await (await client.get('/api/miniapp/push', headers=headers)).json()
            assert again['unreadCount'] == 0 and again['items'][0]['read']
            other = await (await client.get('/api/miniapp/push', headers={'X-Telegram-Init-Data': signed(2)})).json()
            assert other['unreadCount'] == 1 and not other['items'][0]['read']
            bot.send_message.assert_not_awaited()
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())
