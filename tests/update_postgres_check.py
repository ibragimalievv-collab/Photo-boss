"""Run additive migrations and audit checks in a disposable PostgreSQL schema."""
import asyncio
import json
import os
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit_capture import TABLES, install_capture
from app.audit_context import actor_id, install_actor_context
from app.db import Base
from app.models import (
    AuditLog,
    Booking,
    Hotel,
    Notification,
    OperationRequest,
    Package,
    User,
    UserRole,
)
from app.schema_updates import upgrade
from app.workday import Workday


async def main():
    # This separate variable is set only for the disposable CI database.
    url = os.environ['UPDATE_POSTGRES_TEST_URL']
    schema = 'pb_update_test_' + uuid.uuid4().hex
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    install_actor_context(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
            # Reconstruct the pre-update surface without touching other schemas.
            for table in ('hr_candidates', 'academy_assessments', 'work_checklist_completions',
                          'operation_requests', 'guest_feedback', 'shoot_development_reviews',
                          'sales_training_sessions'):
                await conn.execute(text(f'DROP TABLE {table}'))
            for column in ('event_key', 'priority', 'kind', 'payload', 'acknowledged_at', 'resolved_at'):
                await conn.execute(text(f'ALTER TABLE notifications DROP COLUMN {column}'))
            for table in ('shift_check_ins', 'shift_check_outs'):
                await conn.execute(text(f'ALTER TABLE {table} DROP COLUMN offline_claimed_at'))
            for column in ('report_note', 'report_saved_at'):
                await conn.execute(text(f'ALTER TABLE shift_check_outs DROP COLUMN {column}'))
            await conn.execute(text("INSERT INTO users(id,tg_id,name,active,created_at) VALUES (1,9876543210,'Before',true,CURRENT_TIMESTAMP)"))
            await conn.execute(text("INSERT INTO notifications(user_id,text,sent,created_at) VALUES (1,'Legacy notification',true,CURRENT_TIMESTAMP)"))

        migrations = sorted(Path('migrations').glob('00[4-9]_*.sql'))
        assert len(migrations) == 6
        for _ in range(2):
            async with engine.begin() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                for migration in migrations:
                    await driver.execute(migration.read_text())
                await upgrade(conn)
                await install_capture(conn)

        async with factory() as session:
            legacy = await session.scalar(select(Notification))
            assert legacy.text == 'Legacy notification' and legacy.sent
            assert legacy.kind == 'legacy' and legacy.priority == 'info'
            assert (await session.get(User, 1)).tg_id == 9876543210

        token = actor_id.set(1)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE users SET name='After' WHERE id=1"))
            async with factory() as session:
                entry = await session.scalar(select(AuditLog).where(AuditLog.action == 'row.update'))
                assert entry.user_id == 1
                details = json.loads(entry.details)
                assert details['before']['name'] == 'Before' and details['after']['name'] == 'After'
                user = await session.get(User, 1)
                user.name = 'Rolled back'
                await session.flush()
                await session.rollback()
                entries = (await session.scalars(select(AuditLog).where(AuditLog.action == 'row.update'))).all()
                assert len(entries) == 1
        finally:
            actor_id.reset(token)

        async with engine.begin() as conn:
            await conn.execute(text("UPDATE users SET name='Background' WHERE id=1"))
            entry = (await conn.execute(text("SELECT user_id FROM audit_logs WHERE action='row.update' ORDER BY id DESC LIMIT 1"))).one()
            assert entry.user_id is None, 'Actor leaked between transactions'
            triggers = await conn.scalar(text("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND t.tgname LIKE 'pb_audit_%'"), {'schema': schema})
            assert triggers == len(TABLES)
        print('PostgreSQL migrations 004–009 twice, legacy data, audit actor/snapshots/rollback: PASS')

        async with factory() as session:
            session.add_all([UserRole(user_id=1, role='MANAGER'), Hotel(id=1, name='Hotel'),
                             Package(id=1, name='Standard', price_per_photo=400)])
            await session.commit()
        async def rows(conn, sql, **params):
            return list((await conn.execute(text(sql), params)).mappings())
        api = SimpleNamespace(engine=engine, rows=rows, day=date.fromisoformat,
                              today=lambda: date(2026, 9, 22), tz=ZoneInfo('Europe/Moscow'))
        body = {'actorId': 1, 'kind': 'booking', 'key': 'postgres-concurrent-booking', 'date': '2026-09-22',
                'data': {'client_name': 'Guest', 'client_phone': None, 'hotel_id': 1, 'package_id': 1,
                         'room': '100', 'guest_count': 1, 'deposit': '0', 'shoot_date': '2026-09-22',
                         'shoot_time': '12:00', 'photographer_id': None}}
        work = Workday(api)
        results = await asyncio.gather(*(work.execute({'id': 1, 'roles': ['MANAGER']}, body) for _ in range(2)))
        assert results[0].text == results[1].text
        async with factory() as session:
            assert len((await session.scalars(select(Booking))).all()) == 1
            assert len((await session.scalars(select(OperationRequest))).all()) == 1
        print('PostgreSQL concurrent replay: one booking and one operation acknowledgement: PASS')
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
