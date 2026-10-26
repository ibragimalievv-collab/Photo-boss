"""Gallery uploads release DB capacity and recheck their final attachment."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from test_workflow_media_preparation import image, setup

from app.delivery import Delivery
from app.miniapp_security import AccessError
from app.models import DeliveryGallery, DeliveryPhoto

ACTOR = {'id': 2, 'roles': ['PHOTOGRAPHER']}
DATA = {'booking': 1, 'filename': 'guest-original.jpg'}


def test_gallery_preparation_releases_pool_and_final_link_never_calls_provider(tmp_path, monkeypatch):
    async def run():
        engine, _, workday, _ = await setup(tmp_path / 'delivery.sqlite', 'shoot_photo')
        service = Delivery(workday.api)
        entered, release = asyncio.Event(), asyncio.Event()

        async def upload(*args, **kwargs):
            assert engine.pool.checkedout() == 0
            entered.set()
            await release.wait()

        monkeypatch.setattr('app.delivery.configured_from_env', lambda: ('fake-token', 'fake-client'))
        monkeypatch.setattr('app.delivery.YandexDisk.ensure_dir', AsyncMock())
        monkeypatch.setattr('app.delivery.YandexDisk.upload_bytes', upload)
        pending = asyncio.create_task(service.prepare_photo(ACTOR, DATA, image()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with engine.connect() as conn:
                assert await conn.scalar(select(func.count(DeliveryGallery.id))) == 0
            release.set()
            prepared = await pending
            no_io = AsyncMock(side_effect=AssertionError('Provider I/O inside the final transaction'))
            monkeypatch.setattr('app.delivery.YandexDisk.ensure_dir', no_io)
            monkeypatch.setattr('app.delivery.YandexDisk.upload_bytes', no_io)
            monkeypatch.setattr('app.delivery.YandexDisk.metadata', no_io)
            monkeypatch.setattr(Delivery, 'verify_photo', no_io)
            async with AsyncSession(engine) as session:
                saved = await service.apply(session, ACTOR, 'delivery_photo', DATA, image(), prepared=prepared)
                await session.commit()
            async with AsyncSession(engine) as session:
                duplicate = await service.apply(session, ACTOR, 'delivery_photo', DATA, image(), prepared=prepared)
                assert duplicate['duplicate'] and duplicate['photoId'] == saved['photoId']
                assert await session.scalar(select(func.count(DeliveryPhoto.id))) == 1
            no_io.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('change,status', [('assignment', 403), ('folder', 409), ('trash', 409), ('payload', 409)])
def test_gallery_final_link_rejects_changes_after_upload(tmp_path, monkeypatch, change, status):
    async def run():
        engine, _, workday, _ = await setup(tmp_path / 'delivery-race.sqlite', 'shoot_photo')
        service = Delivery(workday.api)
        monkeypatch.setattr('app.delivery.configured_from_env', lambda: ('fake-token', 'fake-client'))
        monkeypatch.setattr('app.delivery.YandexDisk.ensure_dir', AsyncMock())
        monkeypatch.setattr('app.delivery.YandexDisk.upload_bytes', AsyncMock())
        try:
            prepared = await service.prepare_photo(ACTOR, DATA, image())
            async with engine.begin() as conn:
                if change == 'assignment':
                    await conn.execute(text('UPDATE bookings SET photographer_id=3 WHERE id=1'))
                elif change == 'folder':
                    await conn.execute(text("UPDATE clients SET name='Changed guest' WHERE id=1"))
                elif change == 'trash':
                    await conn.execute(text("INSERT INTO settings(key,value) VALUES ('gallery:meta:1',:value)"),
                                       {'value': '{"trashed":true}'})
            async with AsyncSession(engine) as session:
                with pytest.raises(AccessError) as error:
                    await service.apply(session, ACTOR, 'delivery_photo',
                        DATA | {'filename': 'changed.jpg'} if change == 'payload' else DATA, image(), prepared=prepared)
                assert error.value.status == status
                await session.rollback()
            async with AsyncSession(engine) as session:
                assert await session.scalar(select(func.count(DeliveryPhoto.id))) == 0
                assert await session.scalar(select(func.count(DeliveryGallery.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())
