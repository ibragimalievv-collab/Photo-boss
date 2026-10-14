"""QR points to selected-only albums; destructive cleanup is scoped and confirmed."""
import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery import ACTORS, setup

from app.models import Booking, DeliveryPhoto, HotelEmployee, Setting
from app.yandex_disk import ROOT, YandexDisk, YandexDiskError


def test_qr_and_purge_scope_retry_and_revocation(monkeypatch):
    from app import yandex_disk
    monkeypatch.setattr(yandex_disk, 'configured_from_env', lambda: ('fake', 'fake'))
    remove = AsyncMock(side_effect=YandexDiskError('pending'))
    monkeypatch.setattr(YandexDisk, 'delete_confirmed', remove)

    async def run():
        engine, delivery, client, _ = await setup(monkeypatch)
        try:
            async with AsyncSession(engine) as session:
                g = await delivery.ensure(session, ACTORS[1], await session.get(Booking, 1))
                g.published = True
                g.delivery_mode = 'SELECTED'
                token = g.access_token
                gid = g.id
                session.add_all([DeliveryPhoto(gallery_id=gid, uploaded_by_id=2, filename=f'{i}.png',
                    disk_path=ROOT + f'/delivery/booking-1/test/{i}.png', sha256=str(i)*64,
                    byte_size=100, selected=i == 2, upload_set='SELECTED' if i == 2 else 'ALL')
                    for i in (1, 2)])
                await session.commit()
            owner = {'X-Test-Actor': '1'}
            qr = '/api/miniapp/delivery/1/qr'
            assert (await client.get(qr)).status == 401
            assert (await client.get(qr, headers={'X-Test-Actor': '3'})).status == 403
            png = await client.get(qr, headers=owner)
            assert png.status == 200 and (await png.read()).startswith(b'\x89PNG')
            public = '/g/' + token
            data = await (await client.get(public + '/data')).json()
            assert [p['name'] for p in data['photos']] == ['2.png']
            assert (await client.get(public + '/photos/1')).status == 404
            endpoint = '/api/miniapp/galleries/1/purge'
            assert (await client.post(endpoint, headers=owner, json={'confirm': True})).status == 409
            assert (await client.post('/api/miniapp/galleries/1/preferences', headers=owner,
                json={'trashed': True})).status == 200
            assert (await client.get(qr, headers=owner)).status == 409
            for actor in (2, 3, 5):
                assert (await client.post(endpoint, headers={'X-Test-Actor': str(actor)},
                    json={'confirm': True})).status == 403
            assert (await client.post(endpoint, headers=owner, json={'confirm': 1})).status == 400
            async with engine.begin() as connection:
                await connection.execute(HotelEmployee.__table__.delete().where(HotelEmployee.user_id == 4))
            assert (await client.post(endpoint, headers={'X-Test-Actor': '4'}, json={'confirm': True})).status == 403
            remove.assert_not_awaited()
            assert (await client.post(endpoint, headers=owner, json={'confirm': True})).status == 503
            async with AsyncSession(engine) as session:
                assert len(list(await session.scalars(select(DeliveryPhoto)))) == 2
            remove.side_effect = None
            assert (await client.post(endpoint, headers=owner, json={'confirm': True})).status == 200
            remove.assert_awaited_with(ROOT + '/delivery/booking-1')
            assert (await client.post(endpoint, headers=owner, json={'confirm': True})).status == 200
            assert (await client.get(public + '/data')).status == 404
            async with AsyncSession(engine) as session:
                assert not list(await session.scalars(select(DeliveryPhoto)))
                assert await session.get(Booking, 1)
                assert await session.get(Setting, 'gallery:meta:1')
            data = await (await client.get('/api/miniapp/galleries', headers=owner)).json()
            assert data['items'] == []
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_disk_delete_waits_for_confirmed_absence():
    async def run():
        disk = YandexDisk('fake')
        disk.delete = AsyncMock()
        disk.metadata = AsyncMock(return_value={'type': 'dir'})
        try:
            await disk.delete_confirmed(ROOT + '/delivery/booking-1')
        except YandexDiskError:
            pass
        else:
            raise AssertionError('Pending deletion must not be reported complete')
        disk.metadata.side_effect = YandexDiskError('Yandex.Disk HTTP 404')
        await disk.delete_confirmed(ROOT + '/delivery/booking-1')
    asyncio.run(run())
