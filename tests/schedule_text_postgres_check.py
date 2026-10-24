"""PostgreSQL transactions and concurrent retries in a unique disposable schema."""
import asyncio
import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.miniapp_api import MiniApp
from app.miniapp_security import AccessError, role_permissions
from app.schedule_import import ScheduleImport


async def main():
    url = os.environ['UPDATE_POSTGRES_TEST_URL']
    schema = 'pb_schedule_' + uuid4().hex
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    actor = {'id': 1, 'roles': ['OWNER'], 'permissions': role_permissions(['OWNER'])}
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            for ddl in (
                'CREATE TABLE users(id INTEGER PRIMARY KEY,name TEXT,active BOOLEAN)',
                'CREATE TABLE user_roles(user_id INTEGER,role TEXT)',
                'CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)',
                'CREATE TABLE hotels(id INTEGER PRIMARY KEY,name TEXT,active BOOLEAN)',
                'CREATE TABLE hotel_employees(user_id INTEGER,hotel_id INTEGER)',
                'CREATE TABLE shifts(id SERIAL PRIMARY KEY,user_id INTEGER,hotel_id INTEGER,start_at TIMESTAMP,end_at TIMESTAMP,status TEXT)',
                'CREATE TABLE shift_check_ins(id SERIAL PRIMARY KEY,user_id INTEGER,shift_date DATE,started_at TIMESTAMP)',
                'CREATE TABLE shift_check_outs(id SERIAL PRIMARY KEY,user_id INTEGER,shift_date DATE,ended_at TIMESTAMP)',
                'CREATE TABLE work_breaks(id SERIAL PRIMARY KEY,user_id INTEGER,shift_date DATE,started_at TIMESTAMP,ended_at TIMESTAMP)',
                'CREATE TABLE audit_logs(id SERIAL PRIMARY KEY,user_id INTEGER,action TEXT,entity TEXT,entity_id INTEGER,details TEXT,created_at TIMESTAMP)',
            ):
                await conn.execute(text(ddl))
            await conn.execute(text("INSERT INTO users VALUES(1,'Owner fixture',TRUE),(3,'Staff fixture',TRUE)"))
            await conn.execute(text("INSERT INTO user_roles VALUES(1,'OWNER'),(3,'MANAGER'),(3,'PHOTOGRAPHER')"))
            await conn.execute(text("INSERT INTO hotels VALUES(1,'Fixture hotel',TRUE)"))
        api = MiniApp(engine, SimpleNamespace(), [])
        api.today = lambda: date(2026, 10, 24)
        service = ScheduleImport(api)
        body = {'userId': 3, 'text': (Path(__file__).parent/'fixtures/lisa_schedule_20261022.txt').read_text(),
                'defaults': {'hotelId': 1, 'start': '09:00', 'end': '19:00'}}
        uid, parsed, defaults = service.payload(body)
        async with engine.begin() as conn:
            grants = await service.authorize(conn, actor, uid)
            preview, *_ = await service.prepare(conn, actor, uid, parsed, defaults, grants)
        request = {**body, 'revision': preview['revision'], 'requestKey': 'postgres-concurrent-key-001'}
        # SQL rollback includes all daily annotations, shifts and the receipt.
        with patch.object(api, 'audit_write', new=AsyncMock(side_effect=RuntimeError('injected rollback'))):
            try:
                await service.apply_body(actor, request)
                raise AssertionError('Expected failure')
            except RuntimeError:
                pass
        async with engine.connect() as conn:
            assert await conn.scalar(text('SELECT COUNT(*) FROM shifts')) == 0
            assert await conn.scalar(text('SELECT COUNT(*) FROM settings')) == 0
        # Two simultaneous HTTP-equivalent submissions use the same user locks.
        results = await asyncio.gather(service.apply_body(actor, request), service.apply_body(actor, request))
        assert all(r['verified'] and len(r['days']) == 31 for r in results)
        assert results[0] == results[1]
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT COUNT(*) FROM settings WHERE key LIKE 'schedule.day:%'")) == 31
            assert await conn.scalar(text("SELECT COUNT(*) FROM settings WHERE key LIKE 'schedule.import:%'")) == 1
            assert await conn.scalar(text('SELECT COUNT(*) FROM audit_logs')) == 1
            assert await conn.scalar(text('SELECT COUNT(*) FROM user_roles')) == 3
            count = await conn.scalar(text('SELECT COUNT(*) FROM shifts'))
        try:
            await service.apply_body(actor, {**request, 'requestKey': 'postgres-stale-key-0002'})
            raise AssertionError('Stale revision accepted')
        except AccessError as exc:
            assert exc.status == 409
        async with engine.connect() as conn:
            assert await conn.scalar(text('SELECT COUNT(*) FROM shifts')) == count
        print(json.dumps({'status': 'PASS', 'counts': results[0]['counts'],
            'checks': ['PostgreSQL rollback', 'two concurrent identical imports', 'one receipt/audit', 'stale revision rejected', 'access roles preserved']}))
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
