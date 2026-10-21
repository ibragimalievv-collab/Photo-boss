import asyncio
import json
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
    record_charge,
    rules,
    sweep,
)
from app.hotel_points import read_json, revision, save_json
from app.miniapp_security import AccessError
from app.models import (
    Hotel,
    HotelEmployee,
    Notification,
    PayrollEntry,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
)
from app.push_notifications import eligible, payload_for


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


def test_late_notice_is_atomic_scoped_neutral_and_adjusted_once(monkeypatch):
    monkeypatch.setattr('app.push_notifications.utc_now', lambda: NOW.replace(tzinfo=None))

    async def run():
        engine, factory, api = await prepare()
        try:
            async with factory() as session:
                session.add_all([User(id=4,tg_id=4,name='Hotel admin'), UserRole(user_id=4,role='ADMIN'),
                    HotelEmployee(user_id=4,hotel_id=1), Hotel(id=2,name='Other hotel'),
                    User(id=5,tg_id=5,name='Other admin'), UserRole(user_id=5,role='ADMIN'),
                    HotelEmployee(user_id=5,hotel_id=2)])
                await session.commit()
            await initialize(api, NOW)
            await sweep(api, NOW)
            await sweep(api, NOW)
            async with factory() as session:
                notices = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance_fine')))
                assert {n.user_id for n in notices} == {1,3,4}
                assert len(notices) == 3
                for notice in notices:
                    assert 'Нет отметки прихода по графику' in notice.text
                    assert 'не доказывает отсутствие сотрудника' in notice.text
                    assert 'Hotel' in notice.text and '22.09.2026 09:00' in notice.text
                    assert '500.00 ₽' in notice.text
                    assert await eligible(session, notice, SimpleNamespace(active=True,user_id=notice.user_id), for_delivery=True)
                    phone = payload_for(notice)
                    assert 'Нет отметки прихода' in phone['title']
                    assert '500' not in phone['body'] and 'Manager' not in phone['body']
                    assert phone['url'].startswith('/app/#more?attendanceFine=') if notice.user_id != 1 else phone['url'] == '/app/#finance'
                prior_owner = next(n for n in notices if n.user_id == 3)
                prior_admin = next(n for n in notices if n.user_id == 4)
            async with engine.begin() as conn:
                original = await read_json(api, conn, fine_key(1, DAY, 'late'))
            body = {'userId':1,'date':str(DAY),'kind':'late','amount':0,
                    'reason':'Подтверждена уважительная причина','revision':revision(original)}
            await AttendanceFines(api).edit({'miniapp_actor':{'id':3,'roles':['OWNER']},'body':body})
            await sweep(api, NOW)
            async with factory() as session:
                notices = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance_fine')))
                assert len(notices) == 6
                assert len({(n.user_id,n.event_key) for n in notices}) == 6
                assert not await eligible(session, prior_owner, SimpleNamespace(active=True,user_id=3),for_delivery=True)
                assert await eligible(session, prior_owner, SimpleNamespace(active=True,user_id=3))
                adjustments = [n for n in notices if json.loads(n.payload)['noticeRevision'] == 2]
                assert len(adjustments) == 3
                assert all('Начисление: 0.00 ₽ (ранее 500.00 ₽)' in n.text for n in adjustments)
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                assert (await session.scalar(select(PayrollEntry))).amount == 0
                session.add(Shift(user_id=1,hotel_id=2,start_at=dbtime(2026,9,22,12),end_at=dbtime(2026,9,22,15)))
                await session.commit()
                assert not await eligible(session, prior_admin, SimpleNamespace(active=True,user_id=4))
            async with engine.begin() as conn:
                current = await read_json(api, conn, fine_key(1, DAY, 'late'))
            # A reason edit with the same financial result is audited without another alert.
            await AttendanceFines(api).edit({'miniapp_actor':{'id':3,'roles':['OWNER']},
                'body':body | {'revision':revision(current),'reason':'Уточнена уважительная причина'}})
            async with factory() as session:
                assert await session.scalar(select(func.count(Notification.id)).where(Notification.kind == 'attendance_fine')) == 6
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_existing_unchanged_fine_and_rollback_never_create_extra_notice():
    async def run():
        engine, factory, api = await prepare()
        try:
            async with engine.begin() as conn:
                # Existing production state has no notification revision yet.
                await save_json(conn, fine_key(1, DAY, 'late'), {'userId':1,'date':str(DAY),'kind':'late',
                    'label':'Опоздание / нет прихода вовремя','detected':True,'ruleAmount':500,
                    'ruleConfigured':True,'calculatedAmount':500,'expectedAt':'2026-09-22T06:00:00+00:00',
                    'observedAt':None,'amount':500,'override':None,'payrollId':None})
                await reconcile_day(api,conn,1,DAY,NOW)
            async with factory() as session:
                assert await session.scalar(select(func.count(Notification.id))) == 0
            with pytest.raises(RuntimeError, match='rollback fixture'):
                async with engine.begin() as conn:
                    await record_charge(api,conn,2,DAY,'late',True,NOW,NOW,NOW,rule=500)
                    raise RuntimeError('rollback fixture')
            async with factory() as session:
                assert await session.scalar(select(func.count(Notification.id))) == 0
                assert await session.get(Setting,fine_key(2,DAY,'late')) is None
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_new_historical_reconciliation_is_silent_but_manual_adjustment_notifies():
    async def run():
        engine, factory, api = await prepare()
        try:
            async with engine.begin() as conn:
                await reconcile_day(api,conn,1,DAY,datetime(2026,9,23,6,15,tzinfo=timezone.utc))
                value = await read_json(api,conn,fine_key(1,DAY,'late'))
            async with factory() as session:
                assert await session.scalar(select(func.count(Notification.id))) == 0
            await AttendanceFines(api).edit({'miniapp_actor':{'id':3,'roles':['OWNER']},'body':{
                'userId':1,'date':str(DAY),'kind':'late','amount':0,'reason':'Проверено старое начисление',
                'revision':revision(value)}})
            async with factory() as session:
                notices = list(await session.scalars(select(Notification)))
                assert len(notices) == 2 and {n.user_id for n in notices} == {1,3}
                assert all(json.loads(n.payload)['changed'] for n in notices)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_late_arrival_with_unchanged_amount_suppresses_stale_missing_arrival_push(monkeypatch):
    monkeypatch.setattr('app.push_notifications.utc_now', lambda: NOW.replace(tzinfo=None))

    async def run():
        engine, factory, api = await prepare()
        try:
            await initialize(api,NOW)
            await sweep(api,NOW)
            async with factory() as session:
                notice = await session.scalar(select(Notification).where(Notification.user_id == 3))
                assert await eligible(session,notice,SimpleNamespace(active=True,user_id=3),for_delivery=True)
                session.add(ShiftCheckIn(user_id=1,shift_date=DAY,status='STARTED',started_at=NOW.replace(tzinfo=None)))
                await session.commit()
            await sweep(api,NOW)
            async with factory() as session:
                assert await session.scalar(select(func.count(Notification.id))) == 2
                assert not await eligible(session,notice,SimpleNamespace(active=True,user_id=3),for_delivery=True)
                assert await eligible(session,notice,SimpleNamespace(active=True,user_id=3))
        finally:
            await engine.dispose()
    asyncio.run(run())
