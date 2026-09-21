import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_insights import fixture
from test_team import OWNER, Request, setup

from app import development
from app.miniapp_security import AccessError
from app.models import Photo, ShootDevelopmentReview, Shooting
from app.services import development_ai as ai


def test_queue_requires_full_upload_and_sale_and_keeps_history(monkeypatch):
    async def run():
        engine,factory,_=await fixture()
        async with factory() as s:
            shooting=await s.get(Shooting,1)
            shooting.full_upload_completed_at=None
            s.add_all(Photo(shooting_id=1,file_id=f'file{i}') for i in range(7))
            await s.commit()
            assert await development.queue_reviews(s)==0
            from app.models import utc_now
            shooting.full_upload_completed_at=utc_now()
            await s.commit()
            assert await development.queue_reviews(s)==1
            assert await development.queue_reviews(s)==0
        monkeypatch.setattr(development,'config',SimpleNamespace(openai_api_key='test'))
        async def download(file_id,destination,timeout): destination.write(b'\xff\xd8\xfftest')
        async def batch(images): return {'status':'completed','data':{'frames':[{'photo_id':pid} for pid,_ in images],'duplicates':[]}}
        monkeypatch.setattr(ai,'analyze_batch',batch)
        monkeypatch.setattr(ai,'summarize_shoot',AsyncMock(return_value={'status':'completed','data':{'nextShoot':['Change angle']}}))
        for _ in range(3): assert await development.review_one(factory,SimpleNamespace(download=download))
        async with factory() as s:
            r=await s.scalar(select(ShootDevelopmentReview))
            assert r.status=='COMPLETED'
            assert sum(len(b['frames']) for b in json.loads(r.analyzed))==7
            s.add(Photo(shooting_id=1,file_id='new-photo'))
            await s.commit()
            assert await development.queue_reviews(s)==1
            assert len((await s.scalars(select(ShootDevelopmentReview))).all())==2
        await engine.dispose()
    asyncio.run(run())


def test_no_key_does_not_fabricate_ai():
    async def run():
        engine,factory,_=await fixture()
        assert not await development.review_one(factory,SimpleNamespace())
        assert (await ai.roleplay('budget',[],False))['status']=='not_configured'
        await engine.dispose()
    asyncio.run(run())


def test_sales_training_owner_and_revision_boundaries(monkeypatch):
    async def run():
        engine,_factory,team=await setup()
        svc=development.Development(team.api)
        monkeypatch.setattr(development,'config',SimpleNamespace(openai_api_key='test'))
        sid=json.loads((await svc.start(Request(OWNER,{'clientType':'budget'}))).text)['id']
        monkeypatch.setattr(ai,'roleplay',AsyncMock(return_value={'status':'completed','data':{'reply':'Спасибо','score':80,'errors':['Поясните цену'],'recommendations':['Назовите 400 ₽']}}))
        body={'revision':1,'text':'Один кадр 400 ₽','finish':True}
        with pytest.raises(AccessError): await svc.turn(Request({'id':2,'roles':['PHOTOGRAPHER']},body,sid))
        await svc.turn(Request(OWNER,body,sid))
        with pytest.raises(AccessError) as err: await svc.turn(Request(OWNER,body,sid))
        assert err.value.status==409
        await engine.dispose()
    asyncio.run(run())
