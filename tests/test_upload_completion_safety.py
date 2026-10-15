"""Full-shoot uploads must be durable and count originals across both channels."""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_storage_recovery import picture
from test_workflow_services import booking_data, fixture

from app.handlers import photographer
from app.miniapp_security import AccessError
from app.models import Photo, PhotoStorage, ShiftCheckIn, ShiftCheckOut, Shooting, User
from app.services.bookings import create_booking_record
from app.services.photo_storage import receive_photo, unique_photo_count
from app.services.sale_workflow import complete_full_upload, full_shoot_state
from app.services.shifts import shift_now
from app.workflow import Workflow


async def ready_shoot():
    engine, factory = await fixture()
    async with factory() as session:
        booking = await create_booking_record(session, await session.get(User, 1),
                                               booking_data() | {'client_phone': '+79991234567'})
        booking.photographer_id = 2
        booking.status = 'READY_FOR_SALE'
        shooting = await session.scalar(select(Shooting))
        shooting.status = 'READY_FOR_SALE'
        session.add(ShiftCheckIn(user_id=2, shift_date=shift_now().date(),
                                status='STARTED', ai_status='ACCEPTED'))
        await session.commit()
        return engine, factory, shooting.id, booking.id


@pytest.mark.parametrize('status', ['PENDING', 'UPLOADING', 'FAILED', None])
def test_full_upload_waits_for_disk_confirmation(status):
    async def run():
        engine, factory, sid, _bid = await ready_shoot()
        try:
            async with factory() as session:
                photo = Photo(shooting_id=sid, file_id='original')
                session.add(photo)
                await session.flush()
                if status is not None:
                    session.add(PhotoStorage(photo_id=photo.id, shooting_id=sid, telegram_file_id='original',
                                             telegram_unique_id='original', source_kind='DOCUMENT',
                                             status=status, sha256='a'*64))
                await session.flush()
                with pytest.raises(ValueError, match='Яндекс.Диск'):
                    await complete_full_upload(session, await session.get(User, 2), sid)
                assert (await session.get(Shooting, sid)).full_upload_completed_at is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_disk_failure_retry_then_complete(monkeypatch):
    from app.services import photo_storage
    from app.yandex_disk import YandexDiskError

    async def run():
        engine, factory, sid, _bid = await ready_shoot()
        raw = picture()
        monkeypatch.setattr(photo_storage, 'Session', factory)
        async def download(_file, destination, **_kwargs): destination.write(raw)
        class Disk:
            def __init__(self):
                self.state = {'connected': True}
            full = True
            async def ensure_dir(self, _path): pass
            async def upload_bytes(self, _path, _raw, **_kwargs):
                if self.full:
                    raise YandexDiskError('HTTP 507')
        disk = Disk()
        try:
            async with factory() as session:
                await receive_photo(session, shooting_id=sid, file_id='file', file_unique_id='unique',
                                    source_kind='DOCUMENT', raw=raw)
                await session.commit()
            await photo_storage.sync_one(SimpleNamespace(download=download), disk)
            async with factory() as session:
                actor = await session.get(User, 2)
                with pytest.raises(ValueError, match='ошибок 1'):
                    await complete_full_upload(session, actor, sid)
                assert (await session.get(Shooting, sid)).full_upload_completed_at is None
                await Workflow(None).shoot(session, actor, {'PHOTOGRAPHER'}, 'shoot_retry',
                                           {'shooting': {'id': sid}}, None)
                await session.commit()
            disk.full = False
            await photo_storage.sync_one(SimpleNamespace(download=download), disk)
            async with factory() as session:
                result = await complete_full_upload(session, await session.get(User, 2), sid)
                assert result.count == 1 and result.sync['stored'] == 1
                assert result.shooting.full_upload_completed_at is not None
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('first', ['bot', 'app'])
def test_identical_original_is_one_frame_across_bot_and_app(monkeypatch, first):
    async def run():
        engine, factory, sid, _bid = await ready_shoot()
        raw, answers, sent = picture(), [], []
        monkeypatch.setattr(photographer, 'Session', factory)
        async def download(_file, destination, **_kwargs): destination.write(raw)
        async def answer(text, **_kwargs): answers.append(text)
        async def get_data(): return {'shooting_id': sid}
        async def clear(): raise AssertionError('valid upload must keep upload state')
        state = SimpleNamespace(get_data=get_data, clear=clear)
        message = SimpleNamespace(photo=[], from_user=SimpleNamespace(id=2),
            document=SimpleNamespace(file_id='bot-file', file_unique_id='bot-unique',
                                     mime_type=None, file_name='CAMERA.JPG', file_size=len(raw)),
            bot=SimpleNamespace(download=download), answer=answer)
        async def telegram(self, actor, payload, **_kwargs):
            assert payload == raw
            sent.append('sent')
            return 'app-file', 'app-unique'
        monkeypatch.setattr(Workflow, 'save_telegram', telegram)
        async def bot(): await photographer.upload_sale_photo(message, state)
        async def app():
            async with factory() as session:
                result = await Workflow(None).shoot(session, await session.get(User, 2), {'PHOTOGRAPHER'},
                                                     'shoot_photo', {'shooting': {'id': sid}}, raw)
                await session.commit()
                return result
        try:
            if first == 'bot':
                await bot()
                assert (await app())['duplicate']
                assert sent == []
            else:
                assert not (await app())['duplicate']
                await bot()
                assert 'уже есть' in answers[-1]
            async with factory() as session:
                assert await session.scalar(select(func.count(Photo.id))) == 1
                assert await session.scalar(select(func.count(PhotoStorage.id))) == 1
                assert await unique_photo_count(session, sid) == 1
                assert (await session.scalar(select(PhotoStorage))).sha256 == hashlib.sha256(raw).hexdigest()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_legacy_duplicate_does_not_reach_150_frame_commission():
    async def run():
        engine, factory, sid, bid = await ready_shoot()
        try:
            async with factory() as session:
                for number in range(150):
                    photo = Photo(shooting_id=sid, file_id=f'file-{number}')
                    session.add(photo)
                    await session.flush()
                    digest = f'{number if number < 149 else 0:064x}'
                    session.add(PhotoStorage(photo_id=photo.id, shooting_id=sid, telegram_file_id=photo.file_id,
                        telegram_unique_id=f'unique-{number}', source_kind='DOCUMENT', status='STORED',
                        sha256=digest, disk_path=f'app:/PhotoBoss/shootings/{sid}/{digest}.jpg', byte_size=100))
                await session.flush()
                assert await session.scalar(select(func.count(Photo.id))) == 150
                assert (await full_shoot_state(session, bid))[1] == 149
                result = await complete_full_upload(session, await session.get(User, 2), sid)
                assert result.count == 149 and result.commissions['percent'] == 10
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_upload_after_closed_shift_is_denied():
    async def run():
        engine, factory, sid, _bid = await ready_shoot()
        try:
            async with factory() as session:
                session.add(ShiftCheckOut(user_id=2, shift_date=shift_now().date(), status='FINISHED'))
                await session.flush()
                with pytest.raises(AccessError, match='Сначала подтвердите'):
                    await Workflow(None).shoot(session, await session.get(User, 2), {'PHOTOGRAPHER'},
                                               'shoot_photo', {'shooting': {'id': sid}}, picture())
                assert await session.scalar(select(func.count(Photo.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('incomplete_storage', [False, True])
def test_legacy_missing_storage_is_recoverable(monkeypatch, incomplete_storage):
    from app.services import photo_storage

    async def run():
        engine, factory, sid, _bid = await ready_shoot()
        raw = picture()
        monkeypatch.setattr(photo_storage, 'Session', factory)
        async def download(_file, destination, **_kwargs): destination.write(raw)
        class Disk:
            def __init__(self):
                self.state = {'connected': True}
            async def ensure_dir(self, _path): pass
            async def upload_bytes(self, _path, _raw, **_kwargs): pass
        try:
            async with factory() as session:
                photo = Photo(shooting_id=sid, file_id='legacy')
                session.add(photo)
                await session.flush()
                if incomplete_storage:
                    session.add(PhotoStorage(photo_id=photo.id, shooting_id=sid,
                        telegram_file_id='legacy', telegram_unique_id='legacy-incomplete',
                        source_kind='DOCUMENT', status='STORED'))
                await session.flush()
                actor = await session.get(User, 2)
                with pytest.raises(ValueError, match='Яндекс.Диск'):
                    await complete_full_upload(session, actor, sid)
                result = await Workflow(None).shoot(session, actor, {'PHOTOGRAPHER'}, 'shoot_retry',
                                                    {'shooting': {'id': sid}}, None)
                assert result['retried'] == 1
                await session.commit()
            await photo_storage.sync_one(SimpleNamespace(download=download), Disk())
            async with factory() as session:
                result = await complete_full_upload(session, await session.get(User, 2), sid)
                assert result.count == result.sync['stored'] == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
