"""Disposable database and fake storage/bot; no messages or photos leave the test."""
import asyncio
import io
from datetime import date, time, timedelta
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
from app.delivery_bot import phone, preference, start
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Client,
    DeliveryClaim,
    DeliveryContact,
    DeliveryGallery,
    DeliveryPhoto,
    Hotel,
    Package,
    User,
    utc_now,
)

ACTORS = {1:{'id':1,'roles':['OWNER']},2:{'id':2,'roles':['PHOTOGRAPHER']},3:{'id':3,'roles':['PHOTOGRAPHER']},4:{'id':4,'roles':['ADMIN']},5:{'id':5,'roles':['MANAGER']}}


def image(color='orange'):
    output = io.BytesIO()
    Image.new('RGB',(40,30),color).save(output,format='PNG')
    return output.getvalue()


async def setup(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        session.add_all([User(id=i,tg_id=i,name=f'User {i}') for i in ACTORS])
        session.add_all([Client(id=1,name='Guest'),Hotel(id=1,name='Hotel'),Package(id=1,name='Package',price_per_photo=400)])
        await session.flush()
        session.add(Booking(id=1,hotel_id=1,client_id=1,package_id=1,room='200',manager_id=5,photographer_id=2,shoot_date=utc_now().date(),shoot_time=time(12),status='ASSIGNED'))
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
                g.expires_at=utc_now()-timedelta(seconds=1)
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


def test_large_album_upload_and_archive_above_previous_total_limits(monkeypatch):
    from app import delivery
    monkeypatch.setattr(delivery, 'configured_from_env', lambda: ('fake-token', 'fake-client'))
    monkeypatch.setattr(delivery.YandexDisk, 'ensure_dir', AsyncMock())
    upload = AsyncMock()
    monkeypatch.setattr(delivery.YandexDisk, 'upload_bytes', upload)
    async def run():
        engine, svc, client, _ = await setup(monkeypatch)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                booking = await session.get(Booking, 1)
                gallery = await svc.ensure(session, ACTORS[2], booking)
                gallery.published = True
                token = gallery.access_token
                # 150 ordinary 2-MB frames already exceed the former 200-MB cap.
                session.add_all([DeliveryPhoto(gallery_id=gallery.id, uploaded_by_id=2,
                    filename=f'frame-{i}.png', disk_path=f'app:/PhotoBoss/delivery/{i}.png',
                    sha256=f'{i:064x}', byte_size=2*1024*1024) for i in range(150)])
                await session.commit()
                result = await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'next.png'}, image())
                await session.commit()
                assert result['photoId'] and upload.await_count == 1
                assert await session.scalar(select(func.count(DeliveryPhoto.id))) == 151
                # The former count cap must not prevent subsequent uploads either.
                session.add_all([DeliveryPhoto(gallery_id=gallery.id, uploaded_by_id=2,
                    filename=f'frame-{i}.png', disk_path=f'app:/PhotoBoss/delivery/{i}.png',
                    sha256=f'{i:064x}', byte_size=2*1024*1024) for i in range(150, 301)])
                await session.commit()
                await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'another.png'}, image()+b'next-frame')
                await session.commit()
            monkeypatch.setattr(svc, 'bytes', AsyncMock(return_value=image()))
            response = await client.get('/g/'+token+'/album.zip')
            assert response.status == 200
            import zipfile
            with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
                assert len(archive.namelist()) == 303
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_photo_sets_upload_independently_and_deduplicate_within_their_own_folder(monkeypatch):
    from app import delivery
    monkeypatch.setattr(delivery, 'configured_from_env', lambda: ('fake-token', 'fake-client'))
    monkeypatch.setattr(delivery.YandexDisk, 'ensure_dir', AsyncMock())
    upload = AsyncMock()
    monkeypatch.setattr(delivery.YandexDisk, 'upload_bytes', upload)

    async def run():
        engine, svc, client, _ = await setup(monkeypatch)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                # A selected set can be uploaded directly before there is a full set.
                chosen = await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'retouched.png', 'photoSet': 'SELECTED'}, image())
                await session.commit()
                assert upload.await_count == 1
                assert '/Выбранные/' in upload.await_args.args[0]
                assert '/Все фото/' not in upload.await_args.args[0]
                assert await session.scalar(select(func.count(DeliveryPhoto.id))) == 1
                # The same source file must also be allowed in the full set.
                full = await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'source.png', 'photoSet': 'ALL'}, image())
                await session.commit()
                assert chosen['photoId'] != full['photoId']
                assert upload.await_count == 2
                assert '/Все фото/' in upload.await_args.args[0]
                for photo_set, expected in [('ALL', full), ('SELECTED', chosen)]:
                    result = await svc.apply(session, ACTORS[2], 'delivery_photo',
                        {'booking': 1, 'filename': 'renamed.png', 'photoSet': photo_set}, image())
                    assert result['duplicate'] and result['photoId'] == expected['photoId']
                assert upload.await_count == 2
                rows = (await session.scalars(select(DeliveryPhoto).order_by(DeliveryPhoto.id))).all()
                assert len(rows) == 2
                assert [(p.upload_set, p.selected) for p in rows] == [('SELECTED', True), ('ALL', False)]
                assert rows[0].sha256 == rows[1].sha256 and rows[0].disk_path != rows[1].disk_path
            data = await (await client.get('/api/miniapp/delivery/1', headers={'X-Test-Actor': '4'})).json()
            assert [(p['uploadSet'], p['name']) for p in data['photos']] == [
                ('SELECTED', 'retouched.png'), ('ALL', 'source.png')]
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_client_pages_photos_and_archives_follow_independent_photo_sets(monkeypatch):
    import zipfile

    from app import delivery
    monkeypatch.setattr(delivery, 'configured_from_env', lambda: ('fake-token', 'fake-client'))
    monkeypatch.setattr(delivery.YandexDisk, 'ensure_dir', AsyncMock())
    stored = {}

    async def upload(self, path, raw, **kwargs):
        stored[path] = raw

    async def download(photo):
        return stored[photo.disk_path]

    monkeypatch.setattr(delivery.YandexDisk, 'upload_bytes', upload)

    async def run():
        engine, svc, client, _ = await setup(monkeypatch)
        monkeypatch.setattr(svc, 'bytes', download)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                full = await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'all-source.png', 'photoSet': 'ALL'}, image())
                chosen = await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'client-retouched.png', 'photoSet': 'SELECTED'}, image('blue'))
                # Existing marked photos still belong to the full folder and are
                # available in the selected client view for older published albums.
                legacy = await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'legacy-chosen.png', 'selected': True}, image('green'))
                await session.commit()
            headers = {'X-Test-Actor': '4'}
            response = await client.post('/api/miniapp/delivery/1', headers=headers,
                json={'published': True, 'deliveryMode': 'SELECTED'})
            assert response.status == 200
            token = (await response.json())['clientUrl'].rsplit('/', 1)[1]
            base = '/g/' + token
            for mode, allowed, forbidden in [
                ('SELECTED', [(chosen, 'client-retouched.png', image('blue')),
                              (legacy, 'legacy-chosen.png', image('green'))],
                 [(full, 'all-source.png')]),
                ('ALL', [(full, 'all-source.png', image()),
                         (legacy, 'legacy-chosen.png', image('green'))],
                 [(chosen, 'client-retouched.png')]),
            ]:
                response = await client.post('/api/miniapp/delivery/1', headers=headers,
                    json={'deliveryMode': mode})
                assert response.status == 200
                page = await (await client.get(base)).text()
                for photo, name, expected in allowed:
                    assert name in page
                    response = await client.get(base + '/photos/' + str(photo['photoId']) + '?download=1')
                    assert response.status == 200 and await response.read() == expected
                for photo, name in forbidden:
                    assert name not in page
                    for suffix in ['', '?preview=1', '?download=1']:
                        assert (await client.get(base + '/photos/' + str(photo['photoId']) + suffix)).status == 404
                archive = await client.get(base + '/album.zip')
                assert archive.status == 200
                with zipfile.ZipFile(io.BytesIO(await archive.read())) as zipped:
                    assert set(zipped.namelist()) == {str(p['photoId']) + '-' + name for p, name, _ in allowed}
                    for photo, name, expected in allowed:
                        assert zipped.read(str(photo['photoId']) + '-' + name) == expected
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_independent_upload_requires_assignment_and_valid_set_before_publishing(monkeypatch):
    from app import delivery
    monkeypatch.setattr(delivery, 'configured_from_env', lambda: ('fake-token', 'fake-client'))
    monkeypatch.setattr(delivery.YandexDisk, 'ensure_dir', AsyncMock())
    upload = AsyncMock()
    monkeypatch.setattr(delivery.YandexDisk, 'upload_bytes', upload)

    async def run():
        engine, svc, client, _ = await setup(monkeypatch)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                for actor in [3, 5]:
                    with pytest.raises(AccessError) as denied:
                        await svc.apply(session, ACTORS[actor], 'delivery_photo',
                            {'booking': 1, 'filename': 'chosen.png', 'photoSet': 'SELECTED'}, image())
                    assert denied.value.status == 403
                for invalid in ['OTHER', '', None, True, ['SELECTED']]:
                    with pytest.raises(AccessError) as rejected:
                        await svc.apply(session, ACTORS[2], 'delivery_photo',
                            {'booking': 1, 'filename': 'chosen.png', 'photoSet': invalid}, image())
                    assert rejected.value.status == 400
                assert upload.await_count == 0
                # Administrator and owner may upload for another photographer.
                chosen = await svc.apply(session, ACTORS[4], 'delivery_photo',
                    {'booking': 1, 'filename': 'chosen.png', 'photoSet': 'SELECTED'}, image())
                await session.commit()
            headers = {'X-Test-Actor': '1'}
            response = await client.post('/api/miniapp/delivery/1', headers=headers,
                json={'published': True, 'deliveryMode': 'ALL'})
            assert response.status == 409
            response = await client.post('/api/miniapp/delivery/1', headers=headers,
                json={'published': True, 'deliveryMode': 'SELECTED'})
            assert response.status == 200
            # A partial settings update must not expose an empty folder while
            # leaving the gallery marked as published.
            response = await client.post('/api/miniapp/delivery/1', headers=headers,
                json={'deliveryMode': 'ALL'})
            assert response.status == 409
            data = await (await client.get('/api/miniapp/delivery/1', headers=headers)).json()
            assert data['published'] and data['deliveryMode'] == 'SELECTED'
            # Independent files cannot be moved by the legacy checkbox endpoint.
            response = await client.post('/api/miniapp/delivery/1/photos/' + str(chosen['photoId']) + '/selected',
                headers=headers, json={'selected': False})
            assert response.status == 409
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await svc.apply(session, ACTORS[1], 'delivery_photo',
                    {'booking': 1, 'filename': 'full.png', 'photoSet': 'ALL'}, image())
                await session.commit()
                booking = await session.get(Booking, 1)
                booking.photographer_id = 3
                await session.commit()
                with pytest.raises(AccessError) as denied:
                    await svc.apply(session, ACTORS[2], 'delivery_photo',
                        {'booking': 1, 'filename': 'late.png', 'photoSet': 'SELECTED'}, image('blue'))
                assert denied.value.status == 403
            assert upload.await_count == 2
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_legacy_unselect_preserves_a_shared_independently_uploaded_file(monkeypatch):
    import zipfile

    from app import delivery
    monkeypatch.setattr(delivery, 'configured_from_env', lambda: ('fake-token', 'fake-client'))
    monkeypatch.setattr(delivery.YandexDisk, 'ensure_dir', AsyncMock())
    stored, deleted = {}, []

    async def upload(self, path, raw, **kwargs):
        stored[path] = raw

    async def delete(self, path):
        deleted.append(path)
        del stored[path]

    async def download(photo):
        return stored[photo.disk_path]

    monkeypatch.setattr(delivery.YandexDisk, 'upload_bytes', upload)
    monkeypatch.setattr(delivery.YandexDisk, 'delete', delete)

    async def run():
        engine, svc, client, _ = await setup(monkeypatch)
        monkeypatch.setattr(svc, 'bytes', download)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                legacy = await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'legacy.png', 'selected': True}, image())
                chosen = await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'separate.png', 'photoSet': 'SELECTED'}, image())
                unshared = await svc.apply(session, ACTORS[2], 'delivery_photo',
                    {'booking': 1, 'filename': 'legacy-only.png', 'selected': True}, image('blue'))
                await session.commit()
                chosen_row = await session.get(DeliveryPhoto, chosen['photoId'])
                chosen_path = chosen_row.disk_path
                legacy_only_row = await session.get(DeliveryPhoto, unshared['photoId'])
                legacy_only_path = legacy_only_row.disk_path.replace('/Все фото/', '/Выбранные/')
            headers = {'X-Test-Actor': '4'}
            response = await client.post('/api/miniapp/delivery/1', headers=headers,
                json={'published': True, 'deliveryMode': 'SELECTED'})
            assert response.status == 200
            base = '/g/' + (await response.json())['clientUrl'].rsplit('/', 1)[1]

            async def visible_files(expected):
                page = await (await client.get(base)).text()
                assert page.count('<figure>') == len(expected)
                assert page.count('<figcaption>separate.png</figcaption>') == 1
                assert '<figcaption>legacy.png</figcaption>' not in page
                archive = await client.get(base + '/album.zip')
                assert archive.status == 200
                with zipfile.ZipFile(io.BytesIO(await archive.read())) as zipped:
                    assert set(zipped.namelist()) == {str(p['photoId']) + '-' + name for p, name, _ in expected}
                    for photo, name, contents in expected:
                        assert zipped.read(str(photo['photoId']) + '-' + name) == contents

            # Prefer the independent upload over the identical legacy marked
            # frame in both client lists, while keeping old direct links valid.
            await visible_files([(chosen, 'separate.png', image()),
                                 (unshared, 'legacy-only.png', image('blue'))])
            response = await client.get(base + '/photos/' + str(legacy['photoId']) + '?download=1')
            assert response.status == 200 and await response.read() == image()
            for photo in [legacy, unshared]:
                response = await client.post('/api/miniapp/delivery/1/photos/' + str(photo['photoId']) + '/selected',
                    headers=headers, json={'selected': False})
                assert response.status == 200
            # Clean up obsolete legacy copies, but retain the original file owned
            # by the direct SELECTED upload even when its digest is identical.
            assert deleted == [legacy_only_path]
            assert stored[chosen_path] == image()
            response = await client.get(base + '/photos/' + str(chosen['photoId']) + '?download=1')
            assert response.status == 200 and await response.read() == image()
            for photo in [legacy, unshared]:
                assert (await client.get(base + '/photos/' + str(photo['photoId']))).status == 404
            page = await (await client.get(base)).text()
            assert 'separate.png' in page and 'legacy.png' not in page and 'legacy-only.png' not in page
            response = await client.get('/api/miniapp/delivery/1/photos/' + str(legacy['photoId']), headers=headers)
            assert response.status == 200 and await response.read() == image()
            # An old still-open application can mark its full-set copy again;
            # selected clients must continue to receive only one frame per SHA.
            response = await client.post('/api/miniapp/delivery/1/photos/' + str(legacy['photoId']) + '/selected',
                headers=headers, json={'selected': True})
            assert response.status == 200
            await visible_files([(chosen, 'separate.png', image())])
            response = await client.get(base + '/photos/' + str(legacy['photoId']) + '?download=1')
            assert response.status == 200 and await response.read() == image()
        finally:
            await client.close()
            await engine.dispose()
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
