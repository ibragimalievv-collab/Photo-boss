import asyncio
import json
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit_capture import install_capture
from app.audit_context import actor_id, install_actor_context
from app.db import Base
from app.models import AuditLog, ShiftCheckIn, ShiftCheckOut, User


def test_database_snapshots_actor_rollback_and_idempotent_install():
    async def run():
        engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        install_actor_context(engine)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await install_capture(conn)
            await install_capture(conn)
        factory=async_sessionmaker(engine,expire_on_commit=False)
        async with factory() as s:
            s.add(User(id=1,tg_id=1,name='Before'))
            await s.commit()
        token=actor_id.set(1)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE users SET name='After' WHERE id=1"))
            async with factory() as s:
                rows=(await s.scalars(select(AuditLog).where(AuditLog.action=='row.update'))).all()
                assert len(rows)==1 and rows[0].user_id==1
                payload=json.loads(rows[0].details)
                assert payload['before']['name']=='Before' and payload['after']['name']=='After'
                u=await s.get(User,1);u.name='Rollback';await s.flush();await s.rollback()
                rows=(await s.scalars(select(AuditLog).where(AuditLog.action=='row.update'))).all()
                assert len(rows)==1
        finally: actor_id.reset(token)
        await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('model,photo_field,confirmed', [
    (ShiftCheckIn, 'full_body_file_id', 'STARTED'),
    (ShiftCheckOut, 'workplace_file_id', 'FINISHED'),
])
def test_attendance_evidence_stays_out_of_audit_snapshots(model, photo_field, confirmed):
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        install_actor_context(engine)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await install_capture(conn)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add(User(id=1, tg_id=1, name='Photographer'))
                await session.commit()
            token = actor_id.set(1)
            try:
                private_image = 'PRIVATE_CAMERA_EVIDENCE_' * 4096
                async with factory() as session:
                    row = model(user_id=1, shift_date=date(2026, 10, 15), status='AWAITING_PHOTO',
                        photo_data=private_image, **{photo_field: 'private-telegram-file'})
                    session.add(row)
                    await session.commit()
                    row_id = row.id
                async with factory() as session:
                    rows = list(await session.scalars(select(AuditLog).where(AuditLog.entity == model.__tablename__)))
                    assert len(rows) == 1 and rows[0].action == 'row.insert' and rows[0].user_id == 1
                    payload = json.loads(rows[0].details)
                    assert payload['after']['user_id'] == 1
                    assert 'photo_data' not in payload['after'] and photo_field not in payload['after']
                    assert 'PRIVATE_CAMERA_EVIDENCE' not in rows[0].details
                async with engine.begin() as conn:
                    # Replacing only the image must not create a redundant audit event.
                    await conn.execute(text(f'UPDATE {model.__tablename__} SET photo_data=:image WHERE id=:id'),
                        {'image': 'REPLACEMENT_CAMERA_EVIDENCE_' * 4096, 'id': row_id})
                async with factory() as session:
                    rows = list(await session.scalars(select(AuditLog).where(AuditLog.entity == model.__tablename__)))
                    assert len(rows) == 1
                async with engine.begin() as conn:
                    await conn.execute(text(f'UPDATE {model.__tablename__} SET status=:status,photo_data=:image WHERE id=:id'),
                        {'status': confirmed, 'image': private_image, 'id': row_id})
                async with factory() as session:
                    rows = list(await session.scalars(select(AuditLog).where(AuditLog.entity == model.__tablename__).order_by(AuditLog.id)))
                    assert len(rows) == 2 and rows[-1].action == 'row.update' and rows[-1].user_id == 1
                    payload = json.loads(rows[-1].details)
                    assert payload['before']['status'] == 'AWAITING_PHOTO'
                    assert payload['after']['status'] == confirmed
                    for part in ('before', 'after'):
                        assert 'photo_data' not in payload[part] and photo_field not in payload[part]
                    assert all('CAMERA_EVIDENCE' not in row.details for row in rows)
            finally:
                actor_id.reset(token)
        finally:
            await engine.dispose()
    asyncio.run(run())
