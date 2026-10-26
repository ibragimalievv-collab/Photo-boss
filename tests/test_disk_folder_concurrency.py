"""Binding a slow Disk folder leaves connections free and rechecks live access."""
import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_disk_sale_media import prepared
from test_workflow_services import booking_data

from app.disk_sale_media import BINDING, binding
from app.miniapp_api import MiniApp
from app.miniapp_security import AccessError
from app.models import AuditLog, Booking
from app.workday import Workday


async def fixture(path, monkeypatch):
    url = f'sqlite+aiosqlite:///{path}'
    initial, _, feature, request, _, disk, _ = await prepared(monkeypatch, url)
    await initial.dispose()
    engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_timeout=.2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    feature.api.engine = engine
    entered, release = asyncio.Event(), asyncio.Event()
    metadata = disk.disk_metadata.side_effect

    async def delayed(path):
        assert engine.pool.checkedout() == 0, 'Folder metadata retained the application DB connection'
        entered.set()
        await release.wait()
        return await metadata(path)

    disk.disk_metadata.side_effect = delayed
    return engine, factory, feature, request, entered, release


def test_slow_folder_metadata_does_not_block_new_booking_with_one_pool_slot(tmp_path, monkeypatch):
    async def run():
        engine, factory, feature, request, entered, release = await fixture(tmp_path / 'folder.sqlite', monkeypatch)
        api = MiniApp(engine, SimpleNamespace(), [])
        api.today, api.day = lambda: date(2026, 9, 22), date.fromisoformat
        original_rows = api.rows

        async def rows(conn, sql, **params):
            return await original_rows(conn, sql.replace(' FOR UPDATE', ''), **params)

        api.rows = rows
        pending = asyncio.create_task(feature.folder(request))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            body = {'actorId': 1, 'key': 'booking-during-disk-folder', 'kind': 'booking',
                    'date': '2026-09-22', 'data': booking_data() | {'photographer_id': None}}
            response = await asyncio.wait_for(Workday(api).execute({'id': 1, 'roles': ['OWNER']}, body), 1)
            bid = json.loads(response.text)['bookingId']
            assert bid > 1
            release.set()
            assert json.loads((await asyncio.wait_for(pending, 2)).text)['connected']
            async with factory() as session:
                assert await session.scalar(select(func.count(Booking.id))) == 2
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('change', [None, 'revoked', 'binding'])
def test_slow_folder_listing_releases_pool_and_rechecks_live_access(tmp_path, monkeypatch, change):
    async def run():
        engine, _factory, feature, request, entered, release = await fixture(tmp_path / 'listing.sqlite', monkeypatch)
        request.method = 'GET'

        async def delayed_listing(_reader, _path):
            assert engine.pool.checkedout() == 0
            entered.set()
            await release.wait()
            return []

        monkeypatch.setattr('app.disk_sale_media.Reader.listing', delayed_listing)
        pending = asyncio.create_task(feature.files(request))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with engine.begin() as conn:
                # The only pool slot is available while the provider waits.
                assert await conn.scalar(text('SELECT 1')) == 1
                if change == 'revoked':
                    await conn.execute(text('UPDATE users SET active=FALSE WHERE id=1'))
                elif change == 'binding':
                    await conn.execute(text('DELETE FROM settings WHERE key=:key'),
                                       {'key': BINDING + request.match_info['id']})
            release.set()
            if change:
                with pytest.raises(AccessError) as error:
                    await pending
                assert error.value.status == (403 if change == 'revoked' else 409)
            else:
                assert json.loads((await pending).text)['items'] == []
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('change,status', [('revoked', 403), ('roles', 403), ('binding', 409),
                                         ('operations', 409), ('sales', 409)])
def test_delayed_folder_binding_rechecks_changes_before_commit(tmp_path, monkeypatch, change, status):
    async def run():
        engine, factory, feature, request, entered, release = await fixture(tmp_path / 'race.sqlite', monkeypatch)
        async with factory() as session:
            before = await binding(session, request.match_info['id'])
            audits = await session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == 'sale_disk_folder_bound'))
        pending = asyncio.create_task(feature.folder(request))
        expected = before
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with engine.begin() as conn:
                if change == 'revoked':
                    await conn.execute(text('UPDATE users SET active=FALSE WHERE id=1'))
                elif change == 'roles':
                    await conn.execute(text("DELETE FROM user_roles WHERE user_id=1 AND role='OWNER'"))
                elif change == 'binding':
                    expected = before | {'revision': 'newer-owner-choice'}
                    await conn.execute(text('UPDATE settings SET value=:value WHERE key=:key'),
                                       {'key': BINDING + request.match_info['id'], 'value': json.dumps(expected)})
                else:
                    await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value)'),
                                       {'key': change + ':generation', 'value': 'new-generation'})
            release.set()
            with pytest.raises(AccessError) as error:
                await pending
            assert error.value.status == status
            async with factory() as session:
                assert await binding(session, request.match_info['id']) == expected
                assert await session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == 'sale_disk_folder_bound')) == audits
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())
