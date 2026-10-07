"""Disposable database and fake storage/bot; no messages or photos leave the test."""
import asyncio
import io
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import Base
from app.delivery import can_edit, install_delivery, password_hash, password_matches
from app.delivery_bot import start, phone, preference
from app.miniapp_security import AccessError
from app.models import (Booking, Client, DeliveryClaim, DeliveryContact, DeliveryGallery,
                        DeliveryPhoto, Hotel, Package, User)

ACTORS = {1:{'id':1,'roles':['OWNER']},2:{'id':2,'roles':['PHOTOGRAPHER']},3:{'id':3,'roles':['PHOTOGRAPHER']},4:{'id':4,'roles':['ADMIN']},5:{'id':5,'roles':['MANAGER']}}


def image():
    output = io.BytesIO()
    Image.new('RGB',(40,30),'orange').save(output,format='PNG')
    return output.getvalue()


async def setup(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        session.add_all([User(id=i,tg_id=i,name=f'User {i}') for i in ACTORS])
        session.add_all([Client(id=1,name='Guest'),Hotel(id=1,name='Hotel'),Package(id=1,name='Package',price_per_photo=400)])
        await session.flush()
        session.add(Booking(id=1,hotel_id=1,client_id=1,package_id=1,room='200',manager_id=5,photographer_id=2,shoot_date=date.today(),shoot_time=time(12),status='ASSIGNED'))
        await session.commit()
    bot=SimpleNamespace(token='123456:fake-delivery-test-token',me=AsyncMock(return_value=SimpleNamespace(username='PhotoBossTest')),send_message=AsyncMock())
    async def body(request):
        return await request.json()
    api=SimpleNamespace(engine=engine,bot=bot,tz=ZoneInfo('Europe/Moscow'),body=body,day=date.fromisoformat,today=date.today)
    @web.middleware
    async def auth(request, handler):
        if request.path.startswith('/api/miniapp'):
            actor=ACTORS.get(int(request.headers.get('X-Test-Actor','0')))
            if not actor:
                raise web.HTTPUnauthorized()
            request['miniapp_actor']=actor
        try:
            return await handler(request)
        except AccessError as exc:
            return web.json_response({'error':str(exc)},status=exc.status)
    app=web.Application(middlewares=[auth])
    svc=install_delivery(app,api)
    monkeypatch.setattr(svc,'origin',lambda:'https://photos.example')
    client=TestClient(TestServer(app))
    await client.start_server()
    return engine,svc,client,bot


def test_rights_include_owner_admin_and_assigned_photographer_only():
    booking=SimpleNamespace(photographer_id=2)
    assert can_edit(ACTORS[1],booking) and can_edit(ACTORS[4],booking) and can_edit(ACTORS[2],booking)
    assert not can_edit(ACTORS[3],booking) and not can_edit(ACTORS[5],booking)
    value=password_hash('example-password')
    assert password_matches('example-password',value)
    assert not password_matches('wrong-password',value)


def test_private_album_publication_password_expiry_rotation_and_cross_album_access(monkeypatch):
    async def run():
        engine,svc,client,_=await setup(monkeypatch)
        try:
            assert (await client.get('/api/miniapp/delivery/1')).status==401
            for actor in [3,5]:
                with pytest.raises(AccessError):
                    async with AsyncSession(engine) as session:
                        await svc.booking(session,ACTORS[actor],1)
            response=await client.post('/api/miniapp/delivery/1',headers={'X-Test-Actor':'2'},json={'title':'Guest album'})
            assert response.status==200
            data=await response.json()
            assert '?start=booking_' in data['botUrl']
            token=data['clientUrl'].rsplit('/',1)[1]
            assert (await client.get('/g/'+token)).status==404
            response=await client.post('/api/miniapp/delivery/1',headers={'X-Test-Actor':'2'},json={'published':True})
            assert response.status==409
            async with AsyncSession(engine) as session:
                g=await session.scalar(select(DeliveryGallery))
                session.add(DeliveryPhoto(gallery_id=g.id,uploaded_by_id=2,filename='photo.png',disk_path='app:/PhotoBoss/delivery/photo.png',sha256='a'*64,byte_size=len(image())))
                await session.commit()
            response=await client.post('/api/miniapp/delivery/1',headers={'X-Test-Actor':'4'},json={'published':True,'password':'secure-password','expiresDays':7})
            assert response.status==200
            assert '?start=gallery_' in (await response.json())['botUrl']
            page=await client.get('/g/'+token)
            assert 'type="password"' in await page.text()
            assert (await client.get('/g/'+token+'/photos/1')).status==401
            assert (await client.post('/g/'+token+'/unlock',data={'password':'bad'})).status==401
            unlock=await client.post('/g/'+token+'/unlock',data={'password':'secure-password'},allow_redirects=False)
            assert unlock.status==303
            cookie=unlock.headers['Set-Cookie'].split(';',1)[0]
            headers={'Cookie':cookie}
            assert 'Скачать весь альбом' in await (await client.get('/g/'+token,headers=headers)).text()
            assert (await client.get('/g/'+token+'/photos/999',headers=headers)).status==404
            async def content(_):return image()
            monkeypatch.setattr(svc,'bytes',content)
            assert (await client.get('/g/'+token+'/photos/1?download=1',headers=headers)).status==200
            archive=await client.get('/g/'+token+'/album.zip',headers=headers)
            assert archive.status==200 and (await archive.read()).startswith(b'PK')
            async with AsyncSession(engine) as session:
                g=await session.scalar(select(DeliveryGallery))
                assert g.opened_at is not None and g.downloaded_at is not None
            response=await client.post('/api/miniapp/delivery/1',headers={'X-Test-Actor':'1'},json={'rotate':True})
            new=(await response.json())['clientUrl'].rsplit('/',1)[1]
            assert new!=token and (await client.get('/g/'+token)).status==404
            async with AsyncSession(engine) as session:
                g=await session.scalar(select(DeliveryGallery))
                g.expires_at=datetime.utcnow()-timedelta(seconds=1)
                await session.commit()
            assert (await client.get('/g/'+new)).status==404
        finally:
            await client.close();await engine.dispose()
    asyncio.run(run())


def test_upload_is_deduplicated_rechecks_assignment_and_keeps_originals_separate(monkeypatch):
    uploaded=[]
    from app import delivery
    monkeypatch.setattr(delivery,'configured_from_env',lambda:('fake-token','fake-client'))
    async def directory(*args,**kwargs):return {}
    async def upload(self,path,raw,**kwargs):uploaded.append(path)
    monkeypatch.setattr(delivery.YandexDisk,'ensure_dir',directory)
    monkeypatch.setattr(delivery.YandexDisk,'upload_bytes',upload)
    async def run():
        engine,svc,client,_=await setup(monkeypatch)
        try:
            async with AsyncSession(engine,expire_on_commit=False) as session:
                first=await svc.apply(session,ACTORS[2],'delivery_photo',{'booking':1,'filename':'ready.png'},image())
                await session.commit()
                again=await svc.apply(session,ACTORS[2],'delivery_photo',{'booking':1,'filename':'renamed.png'},image())
                assert first['photoId']==again['photoId'] and again['duplicate']
                assert len(uploaded)==1 and '/delivery/booking-1/' in uploaded[0]
                assert await session.scalar(select(func.count(DeliveryPhoto.id)))==1
                with pytest.raises(AccessError):
                    await svc.apply(session,ACTORS[3],'delivery_photo',{'booking':1,'filename':'wrong.png'},image())
                b=await session.get(Booking,1);b.photographer_id=3;await session.commit()
                with pytest.raises(AccessError):
                    await svc.apply(session,ACTORS[2],'delivery_handoff',{'booking':1,'delivered':True})
                with pytest.raises(AccessError):
                    await svc.apply(session,ACTORS[3],'delivery_photo',{'booking':1,'filename':'bad.png'},b'\x89PNG\r\n\x1a\n'+b'x'*200)
        finally:
            await client.close();await engine.dispose()
    asyncio.run(run())


def test_bot_keeps_guests_out_of_staff_tables_and_saves_only_their_own_contact(monkeypatch):
    async def run():
        engine,svc,client,bot=await setup(monkeypatch)
        try:
            async with AsyncSession(engine,expire_on_commit=False) as session:
                b=await session.get(Booking,1);g=await svc.ensure(session,ACTORS[2],b)
                g.published=True;await session.commit();token=g.access_token
            user=SimpleNamespace(id=100001,full_name='Guest Client',username='guest')
            message=SimpleNamespace(bot=bot,from_user=user,answer=AsyncMock())
            await start(message,SimpleNamespace(args='gallery_'+token))
            assert message.answer.call_count==3
            async with AsyncSession(engine) as session:
                assert await session.scalar(select(func.count(User.id)))==5
                assert await session.scalar(select(func.count(DeliveryContact.id)))==1
                assert (await session.scalar(select(DeliveryClaim))).link_sent_at is not None
                assert not (await session.scalar(select(DeliveryContact))).marketing_opt_in
            message.contact=SimpleNamespace(user_id=999,phone_number='+123')
            await phone(message)
            async with AsyncSession(engine) as session:assert (await session.scalar(select(DeliveryContact))).phone is None
            message.contact.user_id=user.id
            await phone(message)
            async with AsyncSession(engine) as session:assert (await session.scalar(select(DeliveryContact))).phone=='+123'
            callback=SimpleNamespace(bot=bot,from_user=user,data='delivery:optin',answer=AsyncMock())
            await preference(callback)
            async with AsyncSession(engine) as session:assert (await session.scalar(select(DeliveryContact))).marketing_opt_in
        finally:
            await client.close();await engine.dispose()
    asyncio.run(run())
