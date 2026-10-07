"""Assignment permissions, concurrent edits and started-shoot protection."""
import asyncio
import json
from datetime import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import Base
from app.miniapp_api import MiniApp
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Client,
    Hotel,
    Package,
    Shooting,
    User,
    UserRole,
    utc_now,
)


class Request(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.match_info = {'id': '1'}


def test_assignment_checks_roles_conflicts_active_photographer_and_started_shoot():
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            session.add_all([User(id=i, tg_id=i, name=f'User {i}', active=i != 6) for i in range(1, 7)])
            session.add_all([UserRole(user_id=i, role='PHOTOGRAPHER') for i in [2, 3, 6]])
            session.add_all([Client(id=1, name='Guest'), Hotel(id=1, name='Hotel'), Package(id=1, name='Package', price_per_photo=400)])
            await session.flush()
            session.add(Booking(id=1, hotel_id=1, client_id=1, package_id=1, room='200', manager_id=1,
                                photographer_id=2, shoot_date=utc_now().date(), shoot_time=time(12), status='ASSIGNED'))
            await session.commit()
        api = object.__new__(MiniApp)
        api.engine = engine
        api.body = AsyncMock(return_value={'photographerId': 3, 'previousPhotographerId': 2})
        async def rows(conn, sql, **params):
            return list((await conn.execute(text(sql.replace(' FOR UPDATE', '')), params)).mappings())
        api.rows = rows
        try:
            for role in ['PHOTOGRAPHER', 'MANAGER']:
                with pytest.raises(AccessError):
                    await api.assign_booking_photographer(Request(miniapp_actor={'id': 1, 'roles': [role]}))
            req = Request(miniapp_actor={'id': 1, 'roles': ['ADMIN']})
            for uid in [5, 6]:
                api.body.return_value = {'photographerId': uid, 'previousPhotographerId': 2}
                with pytest.raises(AccessError, match='активный'):
                    await api.assign_booking_photographer(req)
            api.body.return_value = {'photographerId': 3, 'previousPhotographerId': 2}
            result = await api.assign_booking_photographer(req)
            assert json.loads(result.text)['photographerId'] == 3
            async with AsyncSession(engine) as session:
                assert (await session.get(Booking, 1)).photographer_id == 3
                shoot = await session.scalar(select(Shooting).where(Shooting.booking_id == 1))
                assert shoot.status == 'ASSIGNED'
            # Replay is harmless, and a stale change to another photographer is rejected.
            await api.assign_booking_photographer(req)
            api.body.return_value = {'photographerId': 2, 'previousPhotographerId': None}
            with pytest.raises(AccessError, match='другим'):
                await api.assign_booking_photographer(req)
            async with AsyncSession(engine) as session:
                shoot = await session.scalar(select(Shooting).where(Shooting.booking_id == 1))
                shoot.started_at = utc_now()
                await session.commit()
            api.body.return_value = {'photographerId': 2, 'previousPhotographerId': 3}
            with pytest.raises(AccessError, match='начата'):
                await api.assign_booking_photographer(req)
            api.body.return_value = {'photographerId': True, 'previousPhotographerId': 3}
            with pytest.raises(AccessError):
                await api.assign_booking_photographer(req)
        finally:
            await engine.dispose()
    asyncio.run(run())
