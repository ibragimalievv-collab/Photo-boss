import asyncio
import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text
from test_team import OWNER, Request, setup

from app.miniapp_security import AccessError
from app.models import HRCandidate, ShiftCheckIn, User, UserRole
from app.schema_updates import upgrade
from app.team import contact_key


@pytest.mark.parametrize('a,b', [('@Candidate','https://t.me/candidate'),('+7 (999) 123-45-67','8 999 1234567'),('9991234567','+79991234567')])
def test_contact_identity(a,b):
    assert contact_key(a)==contact_key(b)


def test_duplicate_create_reminders_owner_admin_and_real_start():
    async def run():
        engine,factory,team=await setup()
        async with factory() as session:
            session.add_all([User(id=3,tg_id=3,name='Admin'),UserRole(user_id=3,role='ADMIN')])
            await session.commit()
        body={'name':'Candidate','contact':'@Candidate','source':'Referral','role':'PHOTOGRAPHER','region':'Сочи','responsibleId':3}
        first=json.loads((await team.create(Request(OWNER,body))).text)
        second=json.loads((await team.create(Request(OWNER,body|{'contact':'https://t.me/candidate'}))).text)
        assert first['id']==second['id'] and second['alreadyExists']
        assert json.loads((await team.detail(Request(OWNER,cid=first['id']))).text)['contact']=='@Candidate'
        assert json.loads((await team.listing(Request(OWNER))).text)['externalAutomationEnabled'] is False
        for role in ['PHOTOGRAPHER','MANAGER']:
            for method in [team.listing,team.detail,team.create,team.update,team.hire]:
                with pytest.raises(AccessError) as e:
                    await method(Request({'id':2,'roles':[role]},body,first['id']))
                assert e.value.status==403
        actor={'id':3,'roles':['ADMIN']}
        revision=1
        for stage in ['CONTACTED','INTERVIEW','OFFER','DOCUMENTS']:
            await team.update(Request(actor,{'revision':revision,'stage':stage,'interviewAt':'2026-09-23T09:00:00+03:00','decision':'Manually confirmed','note':'Phone conversation recorded manually','reminderAt':'2026-09-23T08:30:00+03:00','region':'Анапа','responsibleId':3},first['id']))
            revision+=1
        response=json.loads((await team.hire(Request(actor,{'revision':revision,'telegramId':90001,'hotelIds':[1]},first['id']))).text)
        items=json.loads((await team.listing(Request(actor))).text)['items']
        assert items[0]['first_shift_at'] is None and items[0]['responsible_name']=='Admin'
        assert items[0]['region']=='Анапа' and '05:30' in items[0]['reminder_at']
        async with factory() as session:
            session.add(ShiftCheckIn(user_id=response['employeeId'],shift_date=date(2026,9,24),status='STARTED',started_at=datetime(2026,9,24,6,tzinfo=timezone.utc).replace(tzinfo=None)))
            await session.commit()
            assert len((await session.scalars(select(HRCandidate))).all())==1
        assert json.loads((await team.listing(Request(actor))).text)['items'][0]['first_shift_at']
        await engine.dispose()
    asyncio.run(run())


def test_additive_hr_upgrade_preserves_old_candidate_and_repeats():
    async def run():
        engine,_,_=await setup()
        async with engine.begin() as conn:
            await conn.execute(text('DROP TABLE hr_candidates'))
            await conn.execute(text('CREATE TABLE hr_candidates(id INTEGER PRIMARY KEY,name TEXT,contact TEXT)'))
            await conn.execute(text("INSERT INTO hr_candidates VALUES(1,'Existing','@existing')"))
            await upgrade(conn)
            await upgrade(conn)
            row=(await conn.execute(text('SELECT * FROM hr_candidates'))).mappings().one()
            assert dict(row)=={'id':1,'name':'Existing','contact':'@existing','responsible_id':None,'region':'','reminder_at':None}
        await engine.dispose()
    asyncio.run(run())


def test_uncertain_shoot_requires_owner_review_and_preserves_ai_result():

    from app.development import Development
    from app.models import ShootDevelopmentReview

    async def run():
        engine,factory,team=await setup()
        async with factory() as session:
            session.add(ShootDevelopmentReview(id=1,shooting_id=1,photographer_id=2,fingerprint='manual',photo_ids='[1]',status='NEEDS_REVIEW',result='{"nextShoot":["Check focus"]}'))
            await session.commit()
        service=Development(team.api)
        for role in ['ADMIN','PHOTOGRAPHER','MANAGER']:
            with pytest.raises(AccessError):
                await service.resolve_review(Request({'id':2,'roles':[role]},{'comment':'Looks good'},1))
        request=Request(OWNER,{'comment':'Кадр 1: сомнение проверено, повторить фокусировку.'},1)
        await service.resolve_review(request)
        with pytest.raises(AccessError) as error:
            await service.resolve_review(request)
        assert error.value.status==409
        async with factory() as session:
            row=await session.get(ShootDevelopmentReview,1)
            assert row.status=='COMPLETED'
            assert json.loads(row.result)['nextShoot']==['Check focus']
            assert json.loads(row.result)['humanReview']['actorId']==1
        await engine.dispose()
    asyncio.run(run())
