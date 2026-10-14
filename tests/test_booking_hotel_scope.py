"""Exercise cross-hotel read/write denial and immediate grant revocation."""
import asyncio
from datetime import date, time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import Base
from app.delivery import Delivery
from app.miniapp_api import MiniApp
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Client,
    Hotel,
    HotelEmployee,
    Package,
    Shooting,
    User,
    UserRole,
)
from app.photo_edits import access
from app.services.booking_access import (
    booking_scope,
    booking_scope_sql,
    can_access_booking,
)
from app.services.sale_workflow import start_sale_draft
from app.workflow import Workflow


async def setup():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([User(id=i, tg_id=i, name=f'User {i}') for i in range(1, 5)])
        session.add_all([Hotel(id=i, name=f'Hotel {i}') for i in range(1, 4)])
        session.add_all([Client(id=1, name='Guest'), Package(id=1, name='Photo', price_per_photo=400),
                         UserRole(user_id=2, role='PHOTOGRAPHER')])
        await session.flush()
        session.add(HotelEmployee(user_id=1, hotel_id=1))
        for i in range(1, 4):
            session.add(Booking(id=i, hotel_id=i, client_id=1, package_id=1, room='101',
                                manager_id=3 if i == 1 else 4, photographer_id=2 if i != 2 else 4,
                                shoot_date=date(2026, 10, 14), shoot_time=time(12), status='READY_FOR_SALE'))
            session.add(Shooting(id=i, booking_id=i, status='READY_FOR_SALE'))
        await session.commit()
    api = object.__new__(MiniApp)
    api.engine = engine
    api.tz = ZoneInfo("Europe/Moscow")
    api.bot = SimpleNamespace()
    async def rows(conn, sql, **params):
        return list((await conn.execute(text(sql.replace(' FOR UPDATE', '')), params)).mappings())
    api.rows = rows
    return engine, api


@pytest.mark.parametrize('uid,roles,expected', [
    (1, ['OWNER'], [1, 2, 3]), (1, ['ADMIN'], [1]),
    (1, ['ADMIN', 'MANAGER'], [1]), (3, ['MANAGER'], [1]),
    (2, ['PHOTOGRAPHER'], [1, 3]), (4, ['PHOTOGRAPHER'], [2]),
    (4, ['MANAGER'], [2, 3]), (3, [], []),
])
def test_sql_and_orm_enforce_same_current_scope(uid, roles, expected):
    async def run():
        engine, api = await setup()
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                ids = list(await session.scalars(select(Booking.id).where(booking_scope(uid, roles)).order_by(Booking.id)))
                assert ids == expected
            async with engine.connect() as conn:
                sql = booking_scope_sql(uid, roles)
                rows = await api.rows(conn, f'SELECT b.id FROM bookings b WHERE ({sql}) ORDER BY b.id', uid=uid)
                assert [r['id'] for r in rows] == expected
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_admin_cannot_use_foreign_ids_and_revocation_takes_effect():
    async def run():
        engine, api = await setup()
        actor = {'id': 1, 'roles': ['ADMIN']}
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                user = await session.get(User, 1)
                own, foreign = await session.get(Booking, 1), await session.get(Booking, 2)
                assert await can_access_booking(session, 1, ['ADMIN'], own)
                assert not await can_access_booking(session, 1, ['ADMIN'], foreign)
                await Delivery(api).booking(session, actor, 1)
                await access(session, actor, 1)
                for sid in (2, 3):
                    with pytest.raises(AccessError):
                        await Delivery(api).booking(session, actor, sid, edit=True)
                    with pytest.raises(AccessError):
                        await access(session, actor, sid)
                    with pytest.raises(AccessError):
                        await Workflow(api).shoot(session, user, {'ADMIN'}, 'shoot_retry', {'shooting': {'id': sid}}, b'')
                    with pytest.raises(ValueError):
                        await start_sale_draft(session, user, {'ADMIN'}, sid)
                await session.execute(delete(HotelEmployee).where(HotelEmployee.user_id == 1))
                await session.commit()
                assert not await can_access_booking(session, 1, ['ADMIN'], own)
                with pytest.raises(AccessError):
                    await Delivery(api).booking(session, actor, 1)
                with pytest.raises(AccessError):
                    await access(session, actor, 1)
                # Ownership retains access without any hotel grants.
                assert await can_access_booking(session, 1, ['OWNER'], foreign)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_assignment_checks_scope_before_idempotent_replay():
    async def run():
        engine, api = await setup()
        api.body = AsyncMock(return_value={'photographerId': 2, 'previousPhotographerId': 2})
        request = SimpleNamespace(match_info={'id': '1'})
        class Request(dict):
            match_info = request.match_info
        req = Request(miniapp_actor={'id': 1, 'roles': ['ADMIN']})
        try:
            assert (await api.assign_booking_photographer(req)).status == 200
            async with engine.begin() as conn:
                await conn.execute(text('DELETE FROM hotel_employees WHERE user_id=1'))
            with pytest.raises(AccessError) as error:
                await api.assign_booking_photographer(req)
            assert error.value.status == 403
            req['miniapp_actor']['roles'] = ['MANAGER']
            with pytest.raises(AccessError):
                await api.assign_booking_photographer(req)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_employee_card_cannot_bypass_administrator_hotel_scope():
    import json

    from app.team import Team

    async def run():
        engine, api = await setup()
        class Request(dict):
            def __init__(self):
                super().__init__(miniapp_actor={'id': 1, 'roles': ['ADMIN']})
                self.match_info = {'id': '2'}
        request = Request()
        try:
            team = Team(api)
            data = json.loads((await team.overview(request)).text)
            assert [b['id'] for b in data['bookings']] == [1]
            async with engine.begin() as conn:
                await conn.execute(text('DELETE FROM hotel_employees WHERE user_id=1'))
            data = json.loads((await team.overview(request)).text)
            assert data['bookings'] == []
            request['miniapp_actor']['roles'] = ['OWNER']
            data = json.loads((await team.overview(request)).text)
            assert {b['id'] for b in data['bookings']} == {1, 3}
        finally:
            await engine.dispose()
    asyncio.run(run())
