import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from test_team import OWNER, Request, setup

from app.miniapp_security import AccessError
from app.models import (
    GuestFeedback,
    OperationRequest,
    ShiftCheckOut,
    WorkChecklistCompletion,
)
from app.workday import Workday


async def fixture():
    engine,factory,team=await setup()
    team.api.bot=SimpleNamespace(token='123456:test-not-real')
    team.api.day=date.fromisoformat
    return engine,factory,Workday(team.api)


def test_idempotency_and_cross_payload_conflict():
    async def run():
        engine,factory,svc=await fixture()
        await svc.configure(Request(OWNER,{'key':'checklist:equipment','title':'Проверить резервную карту','roles':['OWNER'],'active':True}))
        body={'key':'unique-request-key-1','kind':'checklist','date':'2026-09-21','data':{'itemKey':'checklist:equipment','done':True}}
        for _ in range(2): assert json.loads((await svc.operation(Request(OWNER,body))).text)['status']=='synced'
        with pytest.raises(AccessError) as err:
            await svc.operation(Request(OWNER,body|{'data':body['data']|{'done':False}}))
        assert err.value.status==409
        async with factory() as s:
            assert len((await s.scalars(select(OperationRequest))).all())==1
            assert len((await s.scalars(select(WorkChecklistCompletion))).all())==1
        await engine.dispose()
    asyncio.run(run())


def test_closed_shift_only_and_optimistic_conflicts():
    async def run():
        engine,factory,svc=await fixture()
        body={'key':'unique-request-key-2','kind':'shift_report','date':'2026-09-21','data':{'note':'Need batteries','expectedSavedAt':None}}
        with pytest.raises(AccessError): await svc.operation(Request(OWNER,body))
        async with factory() as s:
            s.add(ShiftCheckOut(user_id=1,shift_date=date(2026,9,21),status='FINISHED'))
            await s.commit()
        await svc.operation(Request(OWNER,body))
        await svc.operation(Request(OWNER,body))
        with pytest.raises(AccessError): await svc.operation(Request(OWNER,body|{'key':'unique-request-key-3'}))
        async with factory() as s:
            assert (await s.scalar(select(ShiftCheckOut))).report_note=='Need batteries'
        await engine.dispose()
    asyncio.run(run())


def test_guest_feedback_private_link_and_one_submission():
    async def run():
        engine,factory,svc=await fixture()
        with pytest.raises(AccessError): await svc.feedback_link(Request({'id':77,'roles':['PHOTOGRAPHER']},cid=1))
        link=json.loads((await svc.feedback_link(Request(OWNER,cid=1))).text)['path']
        assert 'test-not-real' not in link
        request=Request({}, {'rating':5,'comment':'Great'})
        request.match_info={'token':link.rsplit('/',1)[1]}
        assert (await svc.feedback_submit(request)).status==200
        assert (await svc.feedback_submit(request)).status==409
        async with factory() as s: assert (await s.scalar(select(GuestFeedback))).rating==5
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE users SET active=FALSE WHERE id=1"))
        with pytest.raises(AccessError): await svc.operation(Request(OWNER,{'key':'unique-request-key-4','kind':'checklist','date':'2026-09-21','data':{}}))
        await engine.dispose()
    asyncio.run(run())
