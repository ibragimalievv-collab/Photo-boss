"""Slow album downloads must leave the DB available to the rest of Photo Boss.

Disposable PostgreSQL schema; no credentials, messages or provider requests.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from test_delivery import image
from test_miniapp_release import TOKEN, signed

from app import operations_guard, render_main
from app.db import Base
from app.delivery_downloads import MAX_DOWNLOAD_REQUESTS
from app.miniapp_api import install_miniapp
from app.models import (
    Booking,
    Client,
    DeliveryGallery,
    DeliveryPhoto,
    Hotel,
    Package,
    User,
    UserRole,
    utc_now,
)


async def main():
    url = os.environ['UPDATE_POSTGRES_TEST_URL']
    schema = 'pb_download_' + uuid4().hex
    options = {'server_settings': {'search_path': schema}}
    engine = create_async_engine(url, connect_args=options, pool_size=2, max_overflow=0)
    # Match the production unpooled barrier, with the fixture's schema context.
    leases = create_async_engine(url, connect_args=options, poolclass=NullPool)
    operations_guard._lease_engines[engine] = leases
    client = None
    release, all_entered = asyncio.Event(), asyncio.Event()
    pending = []
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            session.add_all([User(id=1, tg_id=1, name='Synthetic Owner'),
                Client(id=1, name='Synthetic Guest'), Hotel(id=1, name='Synthetic Hotel'),
                Package(id=1, name='Synthetic Package', price_per_photo=400)])
            await session.flush()
            session.add_all([UserRole(user_id=1, role='OWNER'), Booking(id=1, manager_id=1,
                hotel_id=1, client_id=1, package_id=1, room='TEST',
                shoot_date=utc_now().date(), shoot_time=utc_now().time())])
            await session.flush()
            gallery = DeliveryGallery(id=1, booking_id=1, access_token='a' * 32,
                title='Synthetic Album', created_by_id=1, published=True, delivery_mode='ALL')
            session.add(gallery)
            await session.flush()
            session.add(DeliveryPhoto(id=1, gallery_id=1, uploaded_by_id=1, filename='synthetic.png',
                disk_path='app:/PhotoBoss/delivery/booking-1/Все фото/synthetic.png', sha256='a' * 64,
                byte_size=len(image()), upload_set='ALL'))
            await session.commit()
        bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock())
        app = web.Application()
        install_miniapp(app, engine=engine, bot=bot, lessons=[])
        app['ready'] = True
        app.router.add_get('/health', render_main.health)
        delivery = app['delivery']
        stream = delivery.stream_archive
        count = 0

        async def observe_stream(*args, **kwargs):
            nonlocal count
            count += 1
            if count == MAX_DOWNLOAD_REQUESTS:
                all_entered.set()
            return await stream(*args, **kwargs)

        async def stalled_bytes(_photo):
            await release.wait()
            return image()

        with (patch.object(delivery, 'bytes', stalled_bytes),
              patch.object(delivery, 'stream_archive', observe_stream),
              patch.object(render_main, 'engine', engine)):
            client = TestClient(TestServer(app))
            await client.start_server()
            path = '/g/' + 'a' * 32 + '/album.zip'
            pending = [asyncio.create_task(client.get(path)) for _ in range(MAX_DOWNLOAD_REQUESTS)]
            await asyncio.wait_for(all_entered.wait(), 5)
            assert engine.pool.checkedout() == 0
            health = await asyncio.wait_for(client.get('/health'), 2)
            assert health.status == 200
            ordinary = await asyncio.wait_for(client.get('/api/miniapp/me',
                headers={'X-Telegram-Init-Data': signed(1)}), 2)
            assert ordinary.status == 200
            excess = await asyncio.wait_for(client.get(path), 4)
            assert excess.status == 503 and excess.headers['Retry-After'] == '2'
            release.set()
            assert all(response.status == 200 for response in await asyncio.gather(*pending))
            assert (await client.get(path)).status == 200
        # The staff selection mutation previously held FOR UPDATE and a pooled
        # connection through Disk I/O. Repeat the same operation while it stalls.
        from app import delivery as delivery_module
        entered_selection = asyncio.Event()
        release.clear()
        async def selection_bytes(_photo):
            entered_selection.set()
            await release.wait()
            return image()
        storage = SimpleNamespace(ensure_dir=AsyncMock(), upload_bytes=AsyncMock(), delete=AsyncMock())
        async with AsyncSession(engine) as session:
            (await session.get(DeliveryGallery, 1)).delivery_mode = 'SELECTED'
            await session.commit()
        with (patch.object(delivery, 'bytes', selection_bytes),
              patch.object(delivery_module, 'configured_from_env', lambda: ('fixture-token', 'fixture-client')),
              patch.object(delivery_module, 'YandexDisk', lambda *_: storage),
              patch.object(render_main, 'engine', engine)):
            path='/api/miniapp/delivery/1/photos/1/selected'
            headers={'X-Telegram-Init-Data': signed(1)}
            pending=[asyncio.create_task(client.post(path,headers=headers,json={'selected':True}))]
            await asyncio.wait_for(entered_selection.wait(),5)
            assert engine.pool.checkedout()==0
            pending.append(asyncio.create_task(client.post(path,headers=headers,json={'selected':True})))
            await asyncio.sleep(.1)
            assert (await asyncio.wait_for(client.get('/health'),2)).status==200
            assert (await asyncio.wait_for(client.get('/api/miniapp/me',headers=headers),2)).status==200
            release.set()
            replies=await asyncio.wait_for(asyncio.gather(*pending),5)
            assert all(r.status==200 for r in replies),[await r.text() for r in replies]
            assert storage.upload_bytes.await_count==1
        print('PASS PostgreSQL: slow selection and concurrent replay retain zero pooled slots; health/login available; one upload')
        assert bot.send_message.await_count == 0
        print('PASS PostgreSQL: 8 slow albums retain zero pooled leases; health and authenticated reads stay 200 with a 2-connection pool; excess download gets retryable 503 and recovery succeeds')
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
        if client:
            await client.close()
        await operations_guard.close_operation_leases(engine)
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
