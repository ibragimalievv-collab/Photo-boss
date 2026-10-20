import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_workflow_replay import setup

from app.attendance_fines import (
    AttendanceFines,
    fine_key,
    initialize,
    reconcile_day,
    rules,
    sweep,
)
from app.hotel_points import read_json, revision, save_json
from app.miniapp_security import AccessError
from app.models import (
    Hotel,
    HotelEmployee,
    PayrollEntry,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
)


def dbtime(*args):
    return datetime(*args, tzinfo=timezone.utc).replace(tzinfo=None)


DAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 6, 15, tzinfo=timezone.utc)


async def prepare():
    engine, factory, work = await setup()
    api = work.api
    api.audit_write = AsyncMock()
    api.body = AsyncMock(side_effect=lambda r:r['body'])
    api.bot = SimpleNamespace()
    async with factory() as s:
        s.add_all([User(id=3,tg_id=3,name='Owner'),UserRole(user_id=3,role='OWNER'),
            Shift(user_id=1,hotel_id=1,start_at=dbtime(2026,9,22,6),end_at=dbtime(2026,9,22,15),status='PLANNED')])
        await s.commit()
    return engine,factory,api


def test_automatic_late_does_not_wait_for_photo_review_and_exception_is_durable():
    async def run():
        engine,factory,api=await prepare()
        service=AttendanceFines(api)
        try:
            await initialize(api,NOW)
            await sweep(api,NOW)
            await sweep(api,NOW)
            async with factory() as s:
                charges=list(await s.scalars(select(PayrollEntry)))
                assert len(charges)==1 and charges[0].amount==-500
                assert await s.scalar(select(func.count(ShiftCheckIn.id)))==0
            async with engine.begin() as conn:
                value=await read_json(api,conn,fine_key(1,DAY,'late'))
            request={'miniapp_actor':{'id':3,'roles':['OWNER']},'body':{'userId':1,'date':str(DAY),
                'kind':'late','amount':0,'reason':'Уважительная причина подтверждена владельцем','revision':revision(value)}}
            await service.edit(request)
            with pytest.raises(AccessError) as error:
                await service.edit(request)
            assert error.value.status==409
            await sweep(api,NOW)
            async with factory() as s:
                rows=list(await s.scalars(select(PayrollEntry)))
                assert len(rows)==1 and rows[0].amount==0
        finally:await engine.dispose()
    asyncio.run(run())


def test_schedule_and_departure_rules_are_independent_and_idempotent():
    async def run():
        engine,factory,api=await prepare()
        try:
            async with factory() as s:
                s.add_all([ShiftCheckIn(user_id=1,shift_date=DAY,status='PENDING_REVIEW',
                    offline_claimed_at=dbtime(2026,9,22,5,55),location_received_at=dbtime(2026,9,22,5,55)),
                    ShiftCheckOut(user_id=1,shift_date=DAY,status='PENDING_REVIEW',
                    offline_claimed_at=dbtime(2026,9,22,14),location_received_at=dbtime(2026,9,22,14))])
                await s.commit()
            async with engine.begin() as conn:
                await save_json(conn,'attendance:fines:rules',{'lateAmount':500,'earlyAmount':300,'missingEndAmount':200})
                for _ in range(2):await reconcile_day(api,conn,1,DAY,datetime(2026,9,22,16,tzinfo=timezone.utc))
            async with factory() as s:
                charges=list(await s.scalars(select(PayrollEntry)))
                assert len(charges)==1 and charges[0].amount==-300
            async with engine.begin() as conn:
                assert await read_json(api,conn,fine_key(1,DAY,'missing_end')) is None
                # Future policy changes cannot rewrite an old event's rate.
                await save_json(conn,'attendance:fines:rules',{'lateAmount':500,'earlyAmount':900,'missingEndAmount':200})
                await reconcile_day(api,conn,1,DAY,datetime(2026,9,22,17,tzinfo=timezone.utc))
            async with factory() as s:assert (await s.scalar(select(PayrollEntry))).amount==-300
        finally:await engine.dispose()
    asyncio.run(run())


def test_history_is_not_charged_and_existing_bot_fee_is_adopted_once():
    async def run():
        engine,factory,api=await prepare()
        try:
            await initialize(api,NOW)
            async with factory() as s:
                s.add_all([Shift(user_id=2,hotel_id=1,start_at=dbtime(2026,9,21,6),end_at=dbtime(2026,9,21,15)),
                    ShiftCheckIn(id=12,user_id=1,shift_date=DAY,status='STARTED',started_at=dbtime(2026,9,22,6,10),late=True,fine_amount=500),
                    PayrollEntry(user_id=1,kind='Штраф за опоздание',amount=-500,period=str(DAY),note='shift_check_in=12; miniapp')])
                await s.commit()
            await sweep(api,NOW)
            await sweep(api,NOW)
            async with factory() as s:
                assert await s.scalar(select(func.count(PayrollEntry.id)))==1
                assert not await s.get(Setting,fine_key(2,date(2026,9,21),'late'))
        finally:await engine.dispose()
    asyncio.run(run())


def test_fine_edits_cannot_cross_hotel_scope_or_edit_self():
    async def run():
        engine,factory,api=await prepare()
        service=AttendanceFines(api)
        try:
            await initialize(api,NOW);await sweep(api,NOW)
            async with factory() as s:
                s.add_all([User(id=4,tg_id=4,name='Other admin'),UserRole(user_id=4,role='ADMIN'),
                    Hotel(id=2,name='Other'),HotelEmployee(user_id=4,hotel_id=2)])
                await s.commit()
            async with engine.begin() as conn:value=await read_json(api,conn,fine_key(1,DAY,'late'))
            body={'userId':1,'date':str(DAY),'kind':'late','amount':0,'reason':'Чужое изменение','revision':revision(value)}
            with pytest.raises(AccessError):await service.edit({'miniapp_actor':{'id':4,'roles':['ADMIN']},'body':body})
            with pytest.raises(AccessError):await service.edit({'miniapp_actor':{'id':3,'roles':['OWNER']},'body':body|{'userId':3}})
            async with engine.begin() as conn:policy=await rules(api,conn)
            request={'miniapp_actor':{'id':4,'roles':['ADMIN']},'body':{'rules':policy,'revision':revision(policy)}}
            with pytest.raises(AccessError):await service.save_rules(request)
        finally:await engine.dispose()
    asyncio.run(run())
