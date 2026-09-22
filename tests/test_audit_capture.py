import asyncio
import json

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit_capture import install_capture
from app.audit_context import actor_id, install_actor_context
from app.db import Base
from app.models import AuditLog, User


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
