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


def test_expired_last_attempt_is_retryable_and_stale_worker_cannot_overwrite(monkeypatch):
    from datetime import timedelta

    from app.models import utc_now
    async def run():
        engine,factory,_=await fixture()
        async with factory() as s:
            s.add(Photo(id=1,shooting_id=1,file_id='frame'))
            s.add(ShootDevelopmentReview(id=1,shooting_id=1,photographer_id=2,fingerprint='expired',photo_ids='[1]',status='RUNNING',attempts=5,claimed_at=utc_now()-timedelta(minutes=6)))
            await s.commit()
        monkeypatch.setattr(development,'config',SimpleNamespace(openai_api_key='test'))
        assert await development.review_one(factory,SimpleNamespace())
        async with factory() as s:
            r=await s.get(ShootDevelopmentReview,1)
            assert r.status=='FAILED' and r.attempts==5
            r.status='PENDING';r.attempts=0;r.claimed_at=None
            await s.commit()
        async def download(_file_id,destination,timeout): destination.write(b'\xff\xd8\xfftest')
        async def supersede(_images):
            async with factory() as s:
                r=await s.get(ShootDevelopmentReview,1)
                r.claimed_at=utc_now()+timedelta(seconds=1)
                r.attempts=2;r.last_error='New worker owns this claim'
                await s.commit()
            return {'status':'completed','data':{'frames':[{'photo_id':1}],'duplicates':[]}}
        monkeypatch.setattr(ai,'analyze_batch',supersede)
        assert await development.review_one(factory,SimpleNamespace(download=download))
        async with factory() as s:
            r=await s.get(ShootDevelopmentReview,1)
            assert r.status=='RUNNING' and r.analyzed=='[]' and r.last_error=='New worker owns this claim'
        await engine.dispose()
    asyncio.run(run())


def test_large_shoot_summary_checkpoints_cover_every_frame(monkeypatch):
    async def run():
        engine,factory,_=await fixture()
        batches=[{'frames':[{'photo_id':i} for i in range(n*6+1,n*6+7)],'duplicates':[]} for n in range(7)]
        async with factory() as s:
            s.add(ShootDevelopmentReview(id=1,shooting_id=1,photographer_id=2,fingerprint='large',photo_ids=json.dumps(list(range(1,43))),analyzed=json.dumps(batches)))
            await s.commit()
        monkeypatch.setattr(development,'config',SimpleNamespace(openai_api_key='test'))
        summary=AsyncMock(return_value={'status':'completed','data':{'nextShoot':['Adjust angle','Vary distance','Check light']}})
        monkeypatch.setattr(ai,'summarize_shoot',summary)
        for _ in range(2): assert await development.review_one(factory,SimpleNamespace())
        async with factory() as s:
            r=await s.get(ShootDevelopmentReview,1)
            parts=json.loads(r.summary_parts)
            assert r.status=='PENDING' and len(parts)==2
            assert [i for p in parts for i in p['frameIds']]==list(range(1,43))
        assert await development.review_one(factory,SimpleNamespace())
        assert len(summary.call_args_list[0].args[0])==6
        assert len(summary.call_args_list[1].args[0])==1
        assert summary.call_args_list[2].args[0]['totalFrames']==42
        async with factory() as s: assert (await s.get(ShootDevelopmentReview,1)).status=='COMPLETED'
        await engine.dispose()
    asyncio.run(run())


def test_malformed_ai_and_provider_failures_never_become_completed_results(monkeypatch):
    async def run():
        valid_frame={'photo_id':1,'angle':'Front','technical':'Visible light','composition':'Center','pose':'Standing','recommendation':'Vary angle','uncertainty':'No EXIF'}
        for data in [{'frames':[None],'duplicates':[]},{'frames':[valid_frame|{'photo_id':True}],'duplicates':[]},
                     {'frames':[valid_frame],'duplicates':[[1,1]]},{'frames':[valid_frame],'duplicates':[[1,99]]}]:
            monkeypatch.setattr(ai,'structured',AsyncMock(return_value={'status':'completed','data':data}))
            assert (await ai.analyze_batch([(1,b'\xff\xd8\xfftest')]))['status']=='unavailable'
        monkeypatch.setattr(ai,'structured',AsyncMock(return_value={'status':'completed','data':{'nextShoot':'fake summary'}}))
        assert (await ai.summarize_shoot([]))['status']=='unavailable'
        monkeypatch.setattr(ai,'structured',AsyncMock(return_value={'status':'completed','data':{'reply':'Fine','score':True,'errors':[],'recommendations':[]}}))
        assert (await ai.roleplay('budget',[],True))['status']=='unavailable'
        engine,factory,team=await setup()
        monkeypatch.setattr(development,'config',SimpleNamespace(openai_api_key='test'))
        service=development.Development(team.api)
        sid=json.loads((await service.start(Request(OWNER,{'clientType':'budget'}))).text)['id']
        monkeypatch.setattr(ai,'roleplay',AsyncMock(return_value={'status':'unavailable'}))
        with pytest.raises(AccessError) as error:
            await service.turn(Request(OWNER,{'revision':1,'text':'One photo costs 400','finish':False},sid))
        assert error.value.status==503
        from app.models import SalesTrainingSession
        async with factory() as s:
            r=await s.get(SalesTrainingSession,sid)
            assert r.revision==1 and len(json.loads(r.transcript))==1
        await engine.dispose()
    asyncio.run(run())
