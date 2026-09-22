import asyncio
import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select
from test_team import OWNER, Request, setup

from app.miniapp_security import AccessError
from app.models import AcademyAssessment, PayrollEntry, Shift, ShiftCheckIn, UserRole
from app.services.discipline import compare
from app.services.onboarding import quiz_for, quiz_kind


def when(day, hour):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc).replace(tzinfo=None)


def test_discipline_uses_original_shifts_and_suspends_conclusions_for_pending_evidence():
    async def run():
        engine, factory, team = await setup()
        try:
            async with factory() as session:
                for day in (14, 15, 16, 17, 18, 19, 20):
                    session.add(Shift(user_id=2, hotel_id=1, start_at=when(day, 6), end_at=when(day, 15)))
                for day in (14, 15, 16):
                    session.add(ShiftCheckIn(user_id=2, shift_date=date(2026,9,day), status='STARTED', late=True, started_at=when(day, 7)))
                session.add(ShiftCheckIn(user_id=2, shift_date=date(2026,9,17), status='PENDING_REVIEW'))
                session.add(ShiftCheckIn(user_id=2, shift_date=date(2026,9,9), status='STARTED', late=True, started_at=when(9, 7)))
                await session.commit()
            async with engine.connect() as conn:
                result = await compare(team.api,conn,date(2026,9,14),date(2026,9,20))
                user = next(r for r in result['items'] if r['id']==2)
                assert user['late']==3 and user['missed']==3 and user['pending']==1
                assert user['changes']['late']=={'current':3,'previous':1,'absolute':2,'percent':200.0}
                assert len(user['repeated'])==2
                assert all(row['date']!='2026-09-17' for row in user['missedShifts'])
                assert result['previousFrom']=='2026-09-07' and result['previousTo']=='2026-09-13'
            async with factory() as session:
                assert not list(await session.scalars(select(PayrollEntry)))
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_onboarding_matches_role_and_progress_comes_from_existing_records():
    async def run():
        engine,factory,team = await setup()
        try:
            manager={'id':1,'roles':['MANAGER']}
            before=json.loads((await team.onboarding(Request(manager))).text)
            assert 'practice' not in {t['key'] for t in before['taskProgress']}
            assert 'lessons' not in {t['key'] for t in before['taskProgress']}
            assert next(t for t in before['taskProgress'] if t['key']=='booking')['completed']
            assert not next(t for t in before['taskProgress'] if t['key']=='sales-practice')['required']
            answers={q['id']:q['answer'] for q in quiz_for(manager['roles'])}
            assert len(quiz_kind(['OWNER','ADMIN','MANAGER','PHOTOGRAPHER'])) <= 40
            assert 'booking' in answers and 'frames' not in answers
            await team.assessment(Request(manager,{'answers':answers}))
            after=json.loads((await team.onboarding(Request(manager))).text)
            assert after['progress']['completed']==before['progress']['completed']+1
            assert next(t for t in after['taskProgress'] if t['key']=='quiz')['completed']
            with pytest.raises(AccessError):
                await team.assessment(Request(manager,{'answers':{q['id']:q['answer'] for q in quiz_for(['OWNER'])}}))
            async with factory() as session:
                assert len(list(await session.scalars(select(AcademyAssessment))))==1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_employee_overview_preserves_admin_financial_scope_and_links_history():
    async def run():
        engine,factory,team = await setup()
        try:
            async with factory() as session:
                session.add_all([UserRole(user_id=2,role='PHOTOGRAPHER'),
                    PayrollEntry(user_id=2,kind='Премия',amount=100,period='2026-09',created_at=when(20,12))])
                await session.commit()
            owner=json.loads((await team.overview(Request(OWNER,cid=2))).text)
            assert owner['finance']['revenue']==80000 and owner['finance']['adjustments']==10000
            assert owner['finance']['saleHistory'][0]['id']==1 and owner['bookings'][0]['id']==1
            assert 'progress' in owner['onboarding'] and 'changes' in owner['discipline']
            admin=json.loads((await team.overview(Request({'id':1,'roles':['ADMIN']},cid=2))).text)
            assert admin['finance']['from']==admin['finance']['to']=='2026-09-21'
            assert admin['finance']['entries']==[] and admin['finance']['saleHistory']==[]
            assert 'fine_amount' not in json.dumps(admin['discipline'])
            with pytest.raises(AccessError):
                await team.discipline(Request({'id':2,'roles':['PHOTOGRAPHER']}))
        finally:
            await engine.dispose()
    asyncio.run(run())
