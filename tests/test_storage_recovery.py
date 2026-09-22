import asyncio
import hashlib
import io
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select
from test_workflow_services import booking_data, fixture

from app import photo_edits
from app.miniapp_security import AccessError
from app.models import Photo, PhotoEdit, PhotoStorage, Shooting, User, utc_now
from app.services import photo_storage
from app.services.bookings import create_booking_record
from app.workflow import Workflow
from app.yandex_disk import YandexDiskError, validate_saved


def picture():
    out = io.BytesIO()
    Image.new('RGB', (40, 30), (100, 120, 140)).save(out, format='JPEG')
    return out.getvalue()


@pytest.mark.parametrize('metadata', [{}, {'type':'file', 'size':1}, {'type':'file','size':3,'sha256':'wrong'}])
def test_unconfirmed_bytes_are_not_saved(metadata):
    with pytest.raises(YandexDiskError):
        validate_saved(metadata, b'abc')


def test_saved_size_and_checksum():
    validate_saved({'type':'file','size':3,'sha256':hashlib.sha256(b'abc').hexdigest()}, b'abc')
    validate_saved({'type':'file','size':3,'md5':hashlib.md5(b'abc').hexdigest()}, b'abc')


async def seeded():
    engine, factory = await fixture()
    raw = picture()
    async with factory() as session:
        booking = await create_booking_record(session, await session.get(User, 1), booking_data())
        booking.photographer_id = 2
        shooting = await session.scalar(select(Shooting))
        shooting.status = 'READY_FOR_SALE'
        photo = Photo(shooting_id=shooting.id, file_id='original')
        session.add(photo)
        await session.flush()
        row = PhotoStorage(photo_id=photo.id, shooting_id=shooting.id, telegram_file_id='original',
                           telegram_unique_id='unique', source_kind='DOCUMENT', status='PENDING', sha256=hashlib.sha256(raw).hexdigest())
        session.add(row)
        await session.commit()
        return engine, factory, shooting.id, photo.id, row.id, raw


def test_quota_failure_retry_and_final_attempt_recovery(monkeypatch):
    async def run():
        engine, factory, sid, _pid, rid, raw = await seeded()
        monkeypatch.setattr(photo_storage, 'Session', factory)
        class Disk:
            def __init__(self):
                self.state = {'connected':True}
                self.full = True
                self.writes = []
            async def ensure_dir(self, path): pass
            async def upload_bytes(self, path, data, **kwargs):
                self.writes.append(path)
                if self.full: raise YandexDiskError('HTTP 507')
                validate_saved({'type':'file','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}, data)
        disk = Disk()
        async def download(fid, destination, **kwargs): destination.write(raw)
        bot = SimpleNamespace(download=download)
        try:
            assert await photo_storage.sync_one(bot, disk)
            async with factory() as session:
                row = await session.get(PhotoStorage, rid)
                assert row.status == 'FAILED' and 'заполнено' in row.last_error
                actor = await session.get(User, 2)
                # Role removal and other photographer cannot retry even knowing the shooting ID.
                for uid, roles in [(2, {'MANAGER'}), (1, {'PHOTOGRAPHER'})]:
                    with pytest.raises(AccessError):
                        await Workflow(None).shoot(session, await session.get(User, uid), roles, 'shoot_retry', {'shooting':{'id':sid}}, None)
                result = await Workflow(None).shoot(session, actor, {'PHOTOGRAPHER'}, 'shoot_retry', {'shooting':{'id':sid}}, None)
                assert result['retried'] == 1
                await session.commit()
            disk.full = False
            await photo_storage.sync_one(bot, disk)
            assert len(set(disk.writes)) == 1
            async with factory() as session:
                row = await session.get(PhotoStorage, rid)
                assert row.status == 'STORED'
                result = await Workflow(None).shoot(session, await session.get(User,2), {'PHOTOGRAPHER'}, 'shoot_retry', {'shooting':{'id':sid}}, None)
                assert result['retried'] == 0
                row.status, row.attempts, row.started_at = 'UPLOADING', 5, utc_now()-timedelta(minutes=11)
                await session.commit()
            assert await photo_storage._claim_job() is None
            async with factory() as session:
                assert (await session.get(PhotoStorage, rid)).status == 'FAILED'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_manual_copy_original_cancel_and_access(monkeypatch):
    async def run():
        engine, factory, sid, pid, rid, raw = await seeded()
        monkeypatch.setattr(photo_edits, 'Session', factory)
        originals = {'app:/PhotoBoss/shootings/1/original.jpg':raw}
        class Disk:
            async def download_bytes(self, path): return originals[path]
            async def ensure_dir(self, path): pass
            async def upload_bytes(self, path, data, **kwargs): originals[path] = data
        try:
            async with factory() as session:
                original = await session.get(PhotoStorage,rid)
                original.status, original.disk_path = 'STORED', next(iter(originals))
                await session.flush()
                actor = {'id':2, 'roles':['PHOTOGRAPHER']}
                data = {'shooting':sid,'photo':pid,'action':'MANUAL','edit':None,'parameters':photo_edits.DEFAULTS|{'exposure':.5}}
                with pytest.raises(AccessError):
                    await photo_edits.apply(session, {'id':1,'roles':['MANAGER']}, data)
                result = await photo_edits.apply(session, actor, data)
                eid = result['editId']
                await session.commit()
            await photo_edits.process_one(Disk())
            async with factory() as session:
                edit = await session.get(PhotoEdit,eid)
                assert edit.status == 'READY'
                assert next(iter(originals.values())) == raw
                assert edit.disk_path != next(iter(originals))
                with Image.open(io.BytesIO(originals[edit.disk_path])) as im:
                    assert im.size == (40,30)
                await photo_edits.apply(session, actor, data|{'action':'cancel','edit':eid})
                await session.commit()
                assert edit.status == 'CANCELLED'
                with pytest.raises(AccessError):
                    await photo_edits.apply(session, actor, data|{'photo':999})
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_correction_bounds_and_no_geometry_changes():
    raw = picture()
    with pytest.raises(AccessError):
        photo_edits.correct(raw, photo_edits.DEFAULTS|{'exposure':float('nan')})
    with pytest.raises(AccessError):
        photo_edits.correct(raw, photo_edits.DEFAULTS|{'face':1})
    result = photo_edits.correct(raw, photo_edits.DEFAULTS)
    assert Image.open(io.BytesIO(result)).size == Image.open(io.BytesIO(raw)).size


def test_ai_failure_does_not_block_manual_and_cancel_wins(monkeypatch):
    async def run():
        engine, factory, _sid, pid, rid, raw = await seeded()
        monkeypatch.setattr(photo_edits, 'Session', factory)
        async def unavailable(*args, **kwargs): return {'status':'unavailable'}
        monkeypatch.setattr(photo_edits, 'structured', unavailable)
        class Disk:
            async def download_bytes(self, path): return raw
            async def ensure_dir(self, path): pass
            async def upload_bytes(self, path, data, **kwargs):
                # User cancels while provider is receiving the copy.
                async with factory() as session:
                    row = await session.scalar(select(PhotoEdit).where(PhotoEdit.mode == 'MANUAL'))
                    row.status = 'CANCELLED'
                    await session.commit()
        try:
            async with factory() as session:
                original = await session.get(PhotoStorage,rid)
                original.status, original.disk_path = 'STORED','app:/PhotoBoss/shootings/1/original.jpg'
                session.add_all([PhotoEdit(photo_id=pid,created_by_id=2,mode='AI'),
                                 PhotoEdit(photo_id=pid,created_by_id=2,mode='MANUAL',parameters=json.dumps(photo_edits.DEFAULTS))])
                await session.commit()
            await photo_edits.process_one(Disk())
            await photo_edits.process_one(Disk())
            async with factory() as session:
                rows = (await session.scalars(select(PhotoEdit).order_by(PhotoEdit.id))).all()
                assert [r.status for r in rows] == ['FAILED','CANCELLED']
                assert rows[1].disk_path is None
                assert (await session.get(PhotoStorage,rid)).sha256 == hashlib.sha256(raw).hexdigest()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_duplicate_reference_is_verified_before_marking_stored(monkeypatch):
    async def run():
        engine, factory, sid, _pid, rid, raw = await seeded()
        monkeypatch.setattr(photo_storage,'Session',factory)
        async def download(fid,destination,**kwargs): destination.write(raw)
        class Disk:
            def __init__(self): self.state, self.repairs = {'connected':True}, []
            async def metadata(self,path): return {'type':'file','size':0,'sha256':'wrong'}
            async def upload_bytes(self,path,payload,**kwargs): self.repairs.append((path,payload))
        disk = Disk()
        try:
            async with factory() as session:
                first = await session.get(PhotoStorage,rid)
                first.status, first.disk_path = 'STORED','app:/PhotoBoss/shootings/1/original.jpg'
                photo = Photo(shooting_id=sid,file_id='same-image')
                session.add(photo)
                await session.flush()
                second = PhotoStorage(photo_id=photo.id,shooting_id=sid,telegram_file_id='same-image',telegram_unique_id='different-telegram-id',source_kind='DOCUMENT',status='PENDING')
                session.add(second)
                await session.commit()
                second_id = second.id
            assert await photo_storage._existing_hash(sid+1,hashlib.sha256(raw).hexdigest(),second_id) is None
            await photo_storage.sync_one(SimpleNamespace(download=download),disk)
            assert disk.repairs == [('app:/PhotoBoss/shootings/1/original.jpg',raw)]
            async with factory() as session:
                assert (await session.get(PhotoStorage,second_id)).status == 'STORED'
        finally:
            await engine.dispose()
    asyncio.run(run())
