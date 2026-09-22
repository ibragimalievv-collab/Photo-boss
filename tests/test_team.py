import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from test_insights import fixture

from app.miniapp_security import AccessError
from app.models import AcademyLessonProgress, HRCandidate, User, UserRole
from app.services.academy_growth import academy_counts
from app.team import Team


class Request(dict):
    def __init__(self, actor, body=None, cid=None):
        super().__init__(miniapp_actor=actor)
        self.body=body or {}
        self.match_info={'id':str(cid)} if cid else {}


async def setup():
    engine,factory,api=await fixture()
    original_rows=api.rows
    async def rows(conn,sql,**params):
        return await original_rows(conn,sql.replace(' FOR UPDATE',''),**params)
    api.rows=rows
    api.body=AsyncMock(side_effect=lambda r:r.body)
    async def audit(conn,actor,action,entity,eid,details):
        await conn.execute(text('INSERT INTO audit_logs(user_id,action,entity,entity_id,details,created_at) VALUES (:uid,:action,:entity,:eid,:details,CURRENT_TIMESTAMP)'),{'uid':actor['id'],'action':action,'entity':entity,'eid':eid,'details':details})
    api.audit_write=audit
    api.bot=SimpleNamespace()
    return engine,factory,Team(api)


OWNER={'id':1,'roles':['OWNER']}


def test_hr_lifecycle_conflicts_and_repeat_hire():
    async def run():
        engine,factory,team=await setup()
        r=await team.create(Request(OWNER,{'name':'Candidate','contact':'@candidate','source':'Referral','role':'PHOTOGRAPHER'}))
        cid=json.loads(r.text)['id']
        revision=1
        for stage in ['CONTACTED','INTERVIEW','OFFER','DOCUMENTS']:
            body={'revision':revision,'stage':stage,'interviewAt':'2026-09-22T12:00:00+03:00','decision':'Confirmed','note':'Conversation note'}
            await team.update(Request(OWNER,body,cid))
            with pytest.raises(AccessError) as err:
                await team.update(Request(OWNER,body,cid))
            assert err.value.status==409
            revision+=1
        hire=Request(OWNER,{'revision':revision,'telegramId':8000000000,'hotelIds':[1]},cid)
        a=json.loads((await team.hire(hire)).text)
        b=json.loads((await team.hire(hire)).text)
        assert a['employeeId']==b['employeeId'] and b['alreadyHired']
        async with factory() as s:
            assert len((await s.scalars(select(User).where(User.tg_id==8000000000))).all())==1
            c=await s.get(HRCandidate,cid)
            assert c.stage=='HIRED' and len(json.loads(c.notes))==4
            assert (await s.scalars(select(UserRole.role).where(UserRole.user_id==c.employee_id))).all()==['PHOTOGRAPHER']
        await engine.dispose()
    asyncio.run(run())


def test_role_boundaries_and_existing_staff_not_overwritten():
    async def run():
        engine,factory,team=await setup()
        for method in [team.listing,team.create,team.update,team.hire]:
            with pytest.raises(AccessError):
                await method(Request({'id':2,'roles':['PHOTOGRAPHER']}))
        with pytest.raises(AccessError):
            await team.overview(Request({'id':2,'roles':['PHOTOGRAPHER']},cid=1))
        async with factory() as s:
            s.add(HRCandidate(id=1,name='Candidate',contact='x',source='test',role='MANAGER',stage='DOCUMENTS',created_by_id=1))
            await s.commit()
        with pytest.raises(AccessError) as e:
            await team.hire(Request(OWNER,{'revision':1,'telegramId':1,'hotelIds':[]},1))
        assert e.value.status==409
        await engine.dispose()
    asyncio.run(run())


def test_guide_does_not_unlock_academy_and_quiz_scored_server_side():
    async def run():
        engine,factory,team=await setup()
        photo={'id':2,'roles':['PHOTOGRAPHER']}
        with pytest.raises(AccessError):
            await team.progress(Request(photo,{'slug':'guide-owner-control'}))
        await team.progress(Request(photo,{'slug':'guide-photographer-shift'}))
        await team.progress(Request(photo,{'slug':'guide-photographer-shift'}))
        async with factory() as s:
            assert (await academy_counts(s,2))[0]==0
            assert len((await s.scalars(select(AcademyLessonProgress))).all())==1
        result=json.loads((await team.assessment(Request(photo,{'answers':{'price':0,'frames':1,'receipt':0,'sync':1}}))).text)
        assert result['score']==50 and not result['passed'] and len(result['errors'])==2
        await engine.dispose()
    asyncio.run(run())
