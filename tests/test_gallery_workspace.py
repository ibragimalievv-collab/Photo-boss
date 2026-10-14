"""New prototype actions keep originals, booking scope and permitted guest downloads."""
import asyncio
import io
import zipfile
from datetime import time
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery import ACTORS, image, setup

from app.models import (
    Booking,
    DeliveryPhoto,
    HotelEmployee,
    Setting,
    utc_now,
)


def test_gallery_actions_shared_favorites_trash_and_private_archives(monkeypatch):
    async def run():
        engine, delivery, client, _ = await setup(monkeypatch)
        delivery.bytes = AsyncMock(return_value=image())
        try:
            async with AsyncSession(engine) as session:
                b = await session.get(Booking, 1)
                g = await delivery.ensure(session, ACTORS[1], b)
                g.published = True
                g.delivery_mode = 'SELECTED'
                session.add_all([DeliveryPhoto(id=1, gallery_id=g.id, uploaded_by_id=2, filename='private.png',
                    disk_path='private', sha256='a'*64, byte_size=40, selected=False, upload_set='ALL'),
                    DeliveryPhoto(id=2, gallery_id=g.id, uploaded_by_id=2, filename='paid.png',
                    disk_path='paid', sha256='b'*64, byte_size=50, selected=True, upload_set='SELECTED')])
                token = g.access_token
                session.add(Booking(id=2, hotel_id=1, client_id=1, package_id=1, room='201', manager_id=5,
                    photographer_id=3, shoot_date=utc_now().date(), shoot_time=time(13)))
                await session.commit()
            owner = {'X-Test-Actor': '1'}
            assert (await client.get('/api/miniapp/galleries')).status == 401
            assert (await client.get('/api/miniapp/galleries/storage', headers={'X-Test-Actor':'2'})).status == 403
            assert (await client.get('/api/miniapp/galleries', headers={'X-Test-Actor':'5'})).status == 200
            for actor, expected in [(1, [1, 2]), (2, [1]), (3, [2]), (4, [1, 2]), (5, [])]:
                rows = await (await client.get('/api/miniapp/galleries', headers={'X-Test-Actor':str(actor)})).json()
                assert sorted(x['id'] for x in rows['items']) == expected
            assert (await client.post('/api/miniapp/galleries/1/preferences', headers={'X-Test-Actor':'3'}, json={'coverId':1})).status == 403
            assert (await client.post('/api/miniapp/galleries/defaults', headers={'X-Test-Actor':'4'}, json={'days':30})).status == 403
            assert (await client.post('/api/miniapp/galleries/defaults', headers=owner, json={'days':90})).status == 200
            assert (await client.post('/api/miniapp/galleries/defaults', headers=owner, json={'days':True})).status == 400
            assert (await client.post('/api/miniapp/galleries/1/preferences', headers=owner, json={'coverId':1, 'favorite':True})).status == 200
            public = '/g/' + token
            data = await (await client.get(public + '/data')).json()
            assert data['coverId'] == 2 and [p['id'] for p in data['photos']] == [2]
            assert 'disk_path' not in str(data) and 'private.png' not in str(data)
            assert (await client.post(public + '/photos/1/favorite', json={'favorite':True})).status == 404
            assert (await client.post(public + '/photos/2/favorite', json={'favorite':True})).status == 200
            data = await (await client.get('/api/miniapp/delivery/1', headers=owner)).json()
            assert next(p for p in data['photos'] if p['id'] == 2)['favorite'] is True
            async with AsyncSession(engine) as session:
                assert (await session.get(DeliveryPhoto, 1)).selected is False
                assert (await session.get(DeliveryPhoto, 2)).selected is True
            assert (await client.get(public + '/album.zip?ids=1')).status == 404
            response = await client.get(public + '/album.zip?ids=2')
            assert response.status == 200
            with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
                assert archive.namelist() == ['2-paid.png']
                assert archive.read('2-paid.png') == image()
            assert (await client.get('/api/miniapp/galleries/1/archive?ids=1', headers={'X-Test-Actor':'3'})).status == 403
            assert (await client.get('/api/miniapp/galleries/1/archive?ids=999', headers=owner)).status == 404
            assert (await client.post('/api/miniapp/galleries/1/preferences', headers=owner, json={'trashed':True})).status == 200
            assert (await client.get(public)).status == 404
            assert (await client.get(public + '/data')).status == 404
            assert (await client.get(public + '/photos/2')).status == 404
            assert (await client.post('/api/miniapp/delivery/1', headers=owner, json={'published':True})).status == 409
            assert (await client.post('/api/miniapp/galleries/1/preferences', headers=owner, json={'trashed':False})).status == 200
            assert (await client.get(public)).status == 200
            stats = await (await client.get('/api/miniapp/galleries', headers=owner)).json()
            row = next(p for p in stats['items'] if p['id']==1)
            assert row['views'] == 1 and row['downloads'] == 1 and row['photoCount'] == 2
            async with engine.begin() as conn:
                await conn.execute(HotelEmployee.__table__.delete().where(HotelEmployee.user_id == 4))
            data = await (await client.get('/api/miniapp/galleries', headers={'X-Test-Actor':'4'})).json()
            assert data['items'] == [] and data['storage']['bytes'] == 0
            async with AsyncSession(engine) as session:
                assert len(list(await session.scalars(select(DeliveryPhoto)))) == 2
                assert await session.get(Setting, 'gallery:defaults')
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())
