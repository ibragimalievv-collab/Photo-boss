"""Full-shoot uploads: camera names, resumable hashes and reuse of selected originals."""
import asyncio
import hashlib
import json

from sqlalchemy import func, select
from test_sales_followups import prepared
from test_workflow_media_preparation import image, setup

from app.miniapp_security import AccessError
from app.models import Photo, PhotoStorage, SaleDraft, SaleDraftPhoto, Shooting
from app.workflow import Workflow, original_name, shoot_fields

PHOTOGRAPHER = {'id': 2, 'roles': ['PHOTOGRAPHER']}


def test_original_name_is_display_only_and_bounded():
    assert original_name(None) is None
    assert original_name('') is None
    assert original_name('C:\\DCIM\\100CANON\\IMG_0042.JPG') == 'IMG_0042.JPG'
    assert original_name('../../etc/passwd') == 'passwd'
    assert original_name('a\x00b\nc.jpg') == 'abc.jpg'
    assert len(original_name('x' * 400 + '.jpg')) == 255
    for bad in (5, ['a'], {'a': 1}):
        try:
            original_name(bad)
        except AccessError as exc:
            assert exc.status == 400
        else:
            raise AssertionError('non-string file name accepted')


def test_shoot_fields_accept_optional_filename_and_strict_hash():
    shoot_fields('shoot_photo', {'shooting': {'id': 1}})
    shoot_fields('shoot_photo', {'shooting': {'id': 1}, 'filename': 'DSC_1.jpg'})
    shoot_fields('shoot_photo_link', {'shooting': {'id': 1}, 'sha256': 'a' * 64})
    shoot_fields('shoot_photo', {'shooting': {'id': 1}, 'source_sha256': 'e' * 64})
    for kind, data in [('shoot_photo', {'shooting': {'id': 1}, 'other': 1}),
                       ('shoot_photo_link', {'shooting': {'id': 1}}),
                       ('shoot_photo_link', {'shooting': {'id': 1}, 'sha256': 'A' * 64}),
                       ('shoot_photo_link', {'shooting': {'id': 1}, 'sha256': 'a' * 63}),
                       ('shoot_photo', {'shooting': {'id': 1}, 'source_sha256': 'nothex' * 8}),
                       ('shoot_photo_link', {'shooting': {'id': 1}, 'sha256': 'a' * 64, 'source_sha256': 'a' * 64})]:
        try:
            shoot_fields(kind, data)
        except AccessError as exc:
            assert exc.status == 400
        else:
            raise AssertionError(f'{kind} accepted {data}')


async def _open_shoot(factory, bid):
    async with factory() as session:
        shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
        shooting.full_upload_completed_at = None
        await session.commit()
        return shooting.id


async def _selected(factory, bid, digest, path='app:/sales/1/selected.jpg'):
    async with factory() as session:
        draft = SaleDraft(booking_id=bid, created_by_id=1, status='COMPLETED')
        session.add(draft)
        await session.flush()
        session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id=path, telegram_unique_id='web-selected:' + digest,
                                   storage_path=path, sha256=digest, byte_size=1234))
        await session.commit()


def test_shoot_photo_stores_camera_file_name(tmp_path, monkeypatch):
    async def run():
        engine, factory, workday, body = await setup(tmp_path / 'name.sqlite', 'shoot_photo')
        try:
            monkeypatch.setattr('app.services.media_files.direct_disk_mode', lambda *_: True)
            monkeypatch.setattr(Workflow, 'save_disk_original', lambda *_: _path('app:/shootings/x/a.jpg'))
            body['data']['filename'] = 'DSC_0123.JPG'
            body['data']['source_sha256'] = 'f' * 64
            result = json.loads((await workday.execute(PHOTOGRAPHER, body, image())).text)
            async with factory() as session:
                photo = await session.get(Photo, result['photoId'])
                assert photo.original_name == 'DSC_0123.JPG'
                assert photo.source_sha256 == 'f' * 64
            from types import SimpleNamespace
            class Req(dict):
                match_info = {'id': str(body['data']['shooting']['id'])}
            hashes = json.loads((await Workflow(workday.api).photo_hashes(Req(miniapp_actor=PHOTOGRAPHER))).text)
            assert 'f' * 64 in hashes['stored'], 'reduced copy is recognised by its original checksum'
        finally:
            await engine.dispose()
    asyncio.run(run())


async def _path(value):
    return value


def test_link_reuses_selected_original_without_bytes(tmp_path):
    async def run():
        url = f"sqlite+aiosqlite:///{tmp_path / 'link.sqlite'}"
        engine, factory, bid = await prepared(url)
        try:
            sid = await _open_shoot(factory, bid)
            digest = hashlib.sha256(b'selected-original').hexdigest()
            await _selected(factory, bid, digest)
            from types import SimpleNamespace
            from datetime import date
            from app.miniapp_api import MiniApp
            from app.workday import Workday
            api = MiniApp(engine, SimpleNamespace(), [])
            api.today = lambda: date(2026, 9, 22)
            api.day = date.fromisoformat
            original_rows = api.rows

            async def rows(conn, sql, **params):
                return await original_rows(conn, sql.replace(' FOR UPDATE', ''), **params)
            api.rows = rows
            workday = Workday(api)
            async with factory() as session:
                before = await session.scalar(select(func.count(Photo.id)).where(Photo.shooting_id == sid))
            body = {'actorId': 2, 'key': 'link-test-key-00000001', 'kind': 'shoot_photo_link', 'date': '2026-09-22',
                    'data': {'shooting': {'id': sid}, 'sha256': digest, 'filename': 'IMG_7.jpg'}}
            first = json.loads((await workday.execute(PHOTOGRAPHER, body)).text)
            assert first['linked'] is True and first['storageStatus'] == 'STORED'
            replay = json.loads((await workday.execute(PHOTOGRAPHER, body)).text)
            assert replay == first, 'same key must replay, not create a second photo'
            again = dict(body, key='link-test-key-00000002')
            second = json.loads((await workday.execute(PHOTOGRAPHER, again)).text)
            assert second['duplicate'] is True and second['photoId'] == first['photoId']
            async with factory() as session:
                assert await session.scalar(select(func.count(Photo.id)).where(Photo.shooting_id == sid)) == before + 1
                photo = await session.get(Photo, first['photoId'])
                storage = await session.scalar(select(PhotoStorage).where(PhotoStorage.photo_id == photo.id))
                assert photo.file_id == storage.disk_path == 'app:/sales/1/selected.jpg'
                assert photo.original_name == 'IMG_7.jpg' and storage.sha256 == digest and storage.byte_size == 1234

            missing = dict(body, key='link-test-key-00000003', data={'shooting': {'id': sid}, 'sha256': 'b' * 64})
            try:
                await workday.execute(PHOTOGRAPHER, missing)
            except AccessError as exc:
                assert exc.status == 409 and 'обычным способом' in str(exc)
            else:
                raise AssertionError('unknown hash must fall back to a normal upload')
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_link_never_reuses_another_bookings_original(tmp_path):
    async def run():
        url = f"sqlite+aiosqlite:///{tmp_path / 'other.sqlite'}"
        engine, factory, bid = await prepared(url)
        try:
            sid = await _open_shoot(factory, bid)
            digest = hashlib.sha256(b'other-guest').hexdigest()
            from test_workflow_services import booking_data
            from app.services.bookings import create_booking_record
            from app.models import User
            async with factory() as session:
                other = await create_booking_record(session, await session.get(User, 1), booking_data())
                await session.commit()
                other_id = other.id
            await _selected(factory, other_id, digest)
            async with factory() as session:
                shooting = await session.get(Shooting, sid)
                from app.models import Booking
                booking = await session.get(Booking, bid)
                actor = await session.get(User, 2)
                try:
                    await Workflow(None).link_selected_original(session, actor, booking, shooting,
                                                                {'shooting': {'id': sid}, 'sha256': digest})
                except AccessError as exc:
                    assert exc.status == 409
                else:
                    raise AssertionError('linked an original of another guest')
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_link_respects_closed_upload(tmp_path):
    async def run():
        url = f"sqlite+aiosqlite:///{tmp_path / 'closed.sqlite'}"
        engine, factory, bid = await prepared(url)  # full upload already completed
        try:
            digest = hashlib.sha256(b'late').hexdigest()
            await _selected(factory, bid, digest)
            async with factory() as session:
                from app.models import Booking, User
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                try:
                    await Workflow(None).link_selected_original(session, await session.get(User, 2),
                        await session.get(Booking, bid), shooting, {'shooting': {'id': shooting.id}, 'sha256': digest})
                except AccessError as exc:
                    assert exc.status == 409 and 'закрыта' in str(exc)
                else:
                    raise AssertionError('closed upload accepted a new photo')
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_photo_hashes_lists_stored_and_reusable_selected(tmp_path):
    async def run():
        url = f"sqlite+aiosqlite:///{tmp_path / 'hashes.sqlite'}"
        engine, factory, bid = await prepared(url)
        try:
            sid = await _open_shoot(factory, bid)
            stored_digest, selected_digest = 'c' * 64, 'd' * 64
            async with factory() as session:
                photo = Photo(shooting_id=sid, file_id='app:/shootings/c.jpg')
                session.add(photo)
                await session.flush()
                session.add(PhotoStorage(photo_id=photo.id, shooting_id=sid, telegram_file_id='app:/shootings/c.jpg',
                                         telegram_unique_id='web-sha256:' + stored_digest, source_kind='DOCUMENT',
                                         status='STORED', sha256=stored_digest))
                await session.commit()
            await _selected(factory, bid, selected_digest)
            await _selected(factory, bid, stored_digest, path='app:/sales/2/also-stored.jpg')
            from types import SimpleNamespace
            from app.miniapp_api import MiniApp
            workflow = Workflow(MiniApp(engine, SimpleNamespace(), []))
            class Req(dict):
                match_info = {'id': str(sid)}
            req = Req(miniapp_actor=PHOTOGRAPHER)
            data = json.loads((await workflow.photo_hashes(req)).text)
            assert data == {'shootingId': sid, 'stored': [stored_digest], 'selected': [selected_digest], 'open': True}
            stranger = Req(miniapp_actor={'id': 3, 'roles': ['PHOTOGRAPHER']})
            try:
                await workflow.photo_hashes(stranger)
            except AccessError as exc:
                assert exc.status == 403
            else:
                raise AssertionError('another employee read shoot hashes')
        finally:
            await engine.dispose()
    asyncio.run(run())
