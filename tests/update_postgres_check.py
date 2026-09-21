"""Run additive migrations and audit checks in a disposable PostgreSQL schema."""
import asyncio
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit_capture import TABLES, install_capture
from app.audit_context import actor_id, install_actor_context
from app.db import Base
from app.models import AuditLog, Notification, User
from app.schema_updates import upgrade


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
            for column in ('report_note', 'report_saved_at'):
                await conn.execute(text(f'ALTER TABLE shift_check_outs DROP COLUMN {column}'))
            await conn.execute(text("INSERT INTO users(id,tg_id,name,active,created_at) VALUES (1,9876543210,'Before',true,CURRENT_TIMESTAMP)"))
            await conn.execute(text("INSERT INTO notifications(user_id,text,sent,created_at) VALUES (1,'Legacy notification',true,CURRENT_TIMESTAMP)"))

        migrations = sorted(Path('migrations').glob('00[4-8]_*.sql'))
        assert len(migrations) == 5
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
        print('PostgreSQL migrations 004–008 twice, legacy data, audit actor/snapshots/rollback: PASS')
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
