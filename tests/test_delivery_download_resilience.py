"""A slow Disk must not preserve permission after revocation or overload DB."""
import asyncio

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery import ACTORS, image, setup

from app.delivery_downloads import MAX_DOWNLOAD_REQUESTS
from app.models import Booking, DeliveryGallery, DeliveryPhoto, UserRole


async def album(engine, delivery):
    async with AsyncSession(engine) as session:
        booking = await session.get(Booking, 1)
        gallery = await delivery.ensure(session, ACTORS[1], booking)
        gallery.published = True
        photo = DeliveryPhoto(gallery_id=gallery.id, uploaded_by_id=2, filename='synthetic.png',
            disk_path='app:/PhotoBoss/delivery/booking-1/synthetic.png', sha256='a' * 64,
            byte_size=len(image()))
        session.add(photo)
        await session.flush()
        result = gallery.access_token, photo.id
        await session.commit()
        return result


@pytest.mark.parametrize('archive', [False, True])
def test_public_download_rechecks_publication_after_slow_provider(monkeypatch, archive):
    async def run():
        engine, delivery, client, _ = await setup(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_bytes(_photo):
            entered.set()
            await release.wait()
            return image()

        monkeypatch.setattr(delivery, 'bytes', slow_bytes)
        try:
            token, photo = await album(engine, delivery)
            path = f'/g/{token}/' + ('album.zip' if archive else f'photos/{photo}?download=1')
            request = asyncio.create_task(client.get(path))
            await asyncio.wait_for(entered.wait(), 2)
            async with AsyncSession(engine) as session:
                gallery = await session.scalar(select(DeliveryGallery))
                gallery.published = False
                await session.commit()
            release.set()
            response = await request
            assert response.status == 404
            assert not (await response.read()).startswith(b'PK')
            async with AsyncSession(engine) as session:
                gallery = await session.scalar(select(DeliveryGallery))
                assert gallery.downloaded_at is None
        finally:
            release.set()
            await client.close()
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize('archive', [False, True])
@pytest.mark.parametrize('revoke', ['assignment', 'role'])
def test_staff_download_rechecks_access_after_slow_provider(monkeypatch, archive, revoke):
    async def run():
        engine, delivery, client, _ = await setup(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_bytes(_photo):
            entered.set()
            await release.wait()
            return image()

        monkeypatch.setattr(delivery, 'bytes', slow_bytes)
        try:
            _token, photo = await album(engine, delivery)
            path = '/api/miniapp/galleries/1/archive' if archive else f'/api/miniapp/delivery/1/photos/{photo}'
            request = asyncio.create_task(client.get(path, headers={'X-Test-Actor': '2'}))
            await asyncio.wait_for(entered.wait(), 2)
            async with AsyncSession(engine) as session:
                if revoke == 'assignment':
                    (await session.get(Booking, 1)).photographer_id = 3
                else:
                    await session.execute(delete(UserRole).where(UserRole.user_id == 2))
                await session.commit()
            release.set()
            assert (await request).status == 403
        finally:
            release.set()
            await client.close()
            await engine.dispose()

    asyncio.run(run())


def test_download_admission_rejects_excess_while_gallery_reads_remain_available(monkeypatch):
    async def run():
        engine, delivery, client, _ = await setup(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def slow_bytes(_photo):
            nonlocal calls
            calls += 1
            if calls == MAX_DOWNLOAD_REQUESTS:
                entered.set()
            await release.wait()
            return image()

        monkeypatch.setattr(delivery, 'bytes', slow_bytes)
        pending = []
        try:
            token, photo = await album(engine, delivery)
            path = f'/g/{token}/photos/{photo}'
            pending = [asyncio.create_task(client.get(path)) for _ in range(MAX_DOWNLOAD_REQUESTS)]
            await asyncio.wait_for(entered.wait(), 2)
            response = await client.get(path)
            assert response.status == 503
            assert response.headers['Retry-After'] == '2'
            assert (await client.get('/api/miniapp/galleries', headers={'X-Test-Actor': '1'})).status == 200
            release.set()
            assert all(response.status == 200 for response in await asyncio.gather(*pending))
            assert (await client.get(path)).status == 200
        finally:
            release.set()
            await asyncio.gather(*pending, return_exceptions=True)
            await client.close()
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("revoke", [False, True])
def test_selection_releases_pool_during_storage_and_rechecks_role(monkeypatch, revoke):
    async def run():
        from sqlalchemy import event

        from app import delivery as module
        engine, delivery, client, _ = await setup(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        held = set()
        event.listen(engine.sync_engine, 'checkout', lambda _db, record, _proxy: held.add(id(record)))
        event.listen(engine.sync_engine, 'checkin', lambda _db, record: held.discard(id(record)))
        async def slow_bytes(_photo):
            entered.set()
            await release.wait()
            return image()
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        monkeypatch.setattr(module, 'configured_from_env', lambda: ('fixture-only', 'fixture-client'))
        monkeypatch.setattr(module, 'YandexDisk', lambda *_: storage)
        monkeypatch.setattr(delivery, 'bytes', slow_bytes)
        pending = None
        try:
            _token, pid = await album(engine, delivery)
            async with AsyncSession(engine) as session:
                photo = await session.get(DeliveryPhoto, pid)
                photo.disk_path = 'app:/PhotoBoss/delivery/booking-1/Все фото/photo.png'
                await session.commit()
            pending = asyncio.create_task(client.post(f'/api/miniapp/delivery/1/photos/{pid}/selected',
                headers={'X-Test-Actor': '2'}, json={'selected': True}))
            await asyncio.wait_for(entered.wait(), 2)
            assert not held, 'Storage waits must not hold a pooled connection'
            if revoke:
                async with AsyncSession(engine) as session:
                    await session.execute(delete(UserRole).where(UserRole.user_id == 2))
                    await session.commit()
            release.set()
            response = await pending
            assert response.status in (403, 404) if revoke else response.status == 200, await response.text()
            async with AsyncSession(engine) as session:
                assert (await session.get(DeliveryPhoto, pid)).selected is (not revoke)
            if revoke:
                storage.upload_bytes.assert_not_awaited()
            else:
                again=await client.post(f'/api/miniapp/delivery/1/photos/{pid}/selected',headers={'X-Test-Actor':'2'},json={'selected':True})
                assert again.status==200
                assert storage.upload_bytes.await_count==1
        finally:
            release.set()
            if pending:
                await asyncio.gather(pending, return_exceptions=True)
            await client.close()
            await engine.dispose()
    asyncio.run(run())
