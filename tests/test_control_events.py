import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from test_team import OWNER, Request
from test_workday import fixture

from app.insights import daily_control
from app.models import (
    Booking,
    Notification,
    Sale,
    Setting,
    ShiftCheckIn,
    ShiftCheckOut,
    UserRole,
)
from app.services.control_notifications import deliver_owner_notifications
from app.services.insights import sync_events


def test_required_checks_start_when_configured_and_events_resolve_and_reopen():
    async def run():
        engine,factory,work = await fixture()
        try:
            await work.configure(Request(OWNER,{'key':'checklist:backup','title':'Резервная карта проверена','roles':['PHOTOGRAPHER'],'active':True,'required':True}))
            state=json.loads((await work.state(Request(OWNER))).text)
            assert not state['items'] and state['configItems'][0]['required']
            async with factory() as s:
                s.add(UserRole(user_id=2,role='PHOTOGRAPHER'))
                s.add_all([ShiftCheckOut(user_id=2,shift_date=date(2026,9,d),status='FINISHED') for d in (20,21)])
                s.add(ShiftCheckIn(user_id=2,shift_date=date(2026,9,21),status='PENDING_REVIEW',offline_claimed_at=datetime(2026,9,21,6,tzinfo=timezone.utc).replace(tzinfo=None)))
                booking=await s.get(Booking,1)
                booking.status='CANCELLED';booking.cancellation_reason='Weather'
                booking.cancelled_at=datetime(2026,9,21,8,tzinfo=timezone.utc).replace(tzinfo=None)
                await s.commit()
            async with engine.begin() as conn:
                events=await sync_events(work.api,conn,1)
                required=[e for e in events if e['key'].startswith('required:')]
                assert len(required)==1 and required[0]['evidence']['date']=='2026-09-21'
                assert any(e['key'].startswith('booking:1:cancelled:') for e in events)
                assert any(e['key'].startswith('attendance:shift_check_ins:') for e in events)
                assert not any(e['key'].startswith('late:') for e in events)
            async with factory() as s:
                notification=await s.scalar(select(Notification).where(Notification.event_key==required[0]['key']))
                notification.acknowledged_at=datetime.now(timezone.utc).replace(tzinfo=None)
                await s.commit()
            body={'actorId':2,'key':'mandatory-check-operation','kind':'checklist','date':'2026-09-21','data':{'itemKey':'checklist:backup','done':True}}
            await work.execute({'id':2,'roles':['PHOTOGRAPHER']},body)
            async with engine.begin() as conn:
                await sync_events(work.api,conn,1)
            async with factory() as s:
                assert (await s.get(Notification,notification.id)).resolved_at
            await work.execute({'id':2,'roles':['PHOTOGRAPHER']},body|{'key':'mandatory-check-operation-2','data':body['data']|{'done':False}})
            async with engine.begin() as conn:
                await sync_events(work.api,conn,1)
            async with factory() as s:
                row=await s.get(Notification,notification.id)
                assert row.resolved_at is None and row.acknowledged_at is None
                assert (await s.get(Sale,1)).amount==800
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_owner_delivery_is_opt_in_batched_throttled_and_excludes_ordinary_events():
    async def run():
        engine,factory,_work=await fixture()
        bot=SimpleNamespace(send_message=AsyncMock())
        now=datetime(2026,9,21,7,tzinfo=timezone.utc)
        try:
            async with factory() as s:
                s.add_all([Notification(user_id=1,text='Daily summary',kind='daily_summary',event_key='daily:2026-09-20'),
                    Notification(user_id=1,text='Critical mismatch',kind='control',priority='critical',event_key='critical:1'),
                    Notification(user_id=1,text='Ordinary event',kind='control',priority='warning',event_key='normal:1')])
                await s.commit()
                assert await deliver_owner_notifications(s,bot,now,'Europe/Moscow')==0
                s.add(Setting(key='notify:owner:1',value=json.dumps({'daily':True,'critical':True})))
                await s.commit()
                assert await deliver_owner_notifications(s,bot,now,'Europe/Moscow')==2
                await s.commit()
                assert bot.send_message.await_count==2
                assert all(call.args[0]==1 and 'Ordinary event' not in call.args[1] for call in bot.send_message.await_args_list)
                s.add(Notification(user_id=1,text='Another critical mismatch',kind='control',priority='critical',event_key='critical:2'))
                await s.commit()
                assert await deliver_owner_notifications(s,bot,now+timedelta(minutes=10),'Europe/Moscow')==0
                assert await deliver_owner_notifications(s,bot,now+timedelta(hours=1),'Europe/Moscow')==1
                await s.commit()
                assert await deliver_owner_notifications(s,bot,now+timedelta(hours=2),'Europe/Moscow')==0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_events_continue_before_daily_summary_hour():
    async def run():
        engine,factory,_work=await fixture()
        try:
            async with factory() as s:
                assert await daily_control(s,datetime(2026,9,21,21,30,tzinfo=timezone.utc),'Europe/Moscow')==0
                await s.commit()
                assert list(await s.scalars(select(Notification).where(Notification.kind=='control')))
                assert not list(await s.scalars(select(Notification).where(Notification.kind=='daily_summary')))
        finally:
            await engine.dispose()
    asyncio.run(run())
