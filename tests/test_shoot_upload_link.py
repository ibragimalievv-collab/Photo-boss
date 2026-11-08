"""Full-shoot uploads: camera names, resumable hashes and reuse of selected originals."""
import asyncio
import hashlib
import json
from typing import ClassVar

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


async def _selected(factory, bid, digest, path='app:/PhotoBoss/sales/1/selected.jpg'):
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
            class Req(dict):
                match_info: ClassVar[dict] = {'id': str(body['data']['shooting']['id'])}
            hashes = json.loads((await Workflow(workday.api).photo_hashes(Req(miniapp_actor=PHOTOGRAPHER))).text)
            assert 'f' * 64 in hashes['stored'], 'reduced copy is recognised by its original checksum'
        finally:
            await engine.dispose()
    asyncio.run(run())


async def _path(value):
    return value


def test_link_reuses_selected_original_without_bytes(tmp_path, monkeypatch):
    async def run():
        url = f"sqlite+aiosqlite:///{tmp_path / 'link.sqlite'}"
        engine, factory, bid = await prepared(url)
        try:
            sid = await _open_shoot(factory, bid)
            digest = hashlib.sha256(b'selected-original').hexdigest()
            await _selected(factory, bid, digest)
            from datetime import date
            from types import SimpleNamespace

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
            copies = []

            async def copy(_self, source, folder, digest, size):
                copies.append((source, folder, digest, size))
                return f'app:/PhotoBoss{folder}/{digest}.jpg'
            monkeypatch.setattr(Workflow, 'copy_disk_original', copy)
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
                copied = f'app:/PhotoBoss/shootings/{sid}/{digest}.jpg'
                assert photo.file_id == storage.disk_path == copied, 'the archive keeps its own copy in the shoot folder'
                assert copies == [('app:/PhotoBoss/sales/1/selected.jpg', f'/shootings/{sid}', digest, 1234)]
                assert photo.original_name == 'IMG_7.jpg' and storage.sha256 == digest and storage.byte_size == 1234
                # The fixture's other frames have no storage receipts; only the
                # linked one is checked here against the completion rules.
                from sqlalchemy import delete
                await session.execute(delete(Photo).where(Photo.shooting_id == sid, Photo.id != photo.id))
                from app.services.photo_storage import require_confirmed_originals
                assert await require_confirmed_originals(session, sid) == 1, 'a linked frame must not block completion'
                await session.rollback()

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

            from app.models import User
            from app.services.bookings import create_booking_record
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
            await _selected(factory, bid, stored_digest, path='app:/PhotoBoss/sales/2/also-stored.jpg')
            from types import SimpleNamespace

            from app.miniapp_api import MiniApp
            workflow = Workflow(MiniApp(engine, SimpleNamespace(), []))
            class Req(dict):
                match_info: ClassVar[dict] = {'id': str(sid)}
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


def test_disk_copy_is_confirmed_by_size_and_checksum(monkeypatch):
    from unittest.mock import AsyncMock

    from app.yandex_disk import YandexDisk, YandexDiskError
    digest = 'a' * 64
    good = {'type': 'file', 'size': 2048, 'sha256': digest}
    missing = YandexDiskError('Yandex.Disk HTTP 404: DiskNotFoundError', http_status=404)

    async def run(metadata, expect_post):
        disk = YandexDisk('synthetic')
        disk.metadata = AsyncMock(side_effect=metadata)
        disk._json = AsyncMock(return_value={})
        try:
            return await disk.copy('app:/PhotoBoss/sales/1/x.jpg', 'app:/PhotoBoss/shootings/2/x.jpg',
                                   size=2048, sha256=digest, wait_seconds=0)
        finally:
            assert disk._json.await_count == (1 if expect_post else 0)

    # Identical target already there: accepted without another copy.
    assert asyncio.run(run([good], False)) == 'app:/PhotoBoss/shootings/2/x.jpg'
    # Missing target: copied once and confirmed.
    assert asyncio.run(run([missing, good], True)) == 'app:/PhotoBoss/shootings/2/x.jpg'
    # A different file must never be overwritten or accepted.
    try:
        asyncio.run(run([{'type': 'file', 'size': 999, 'sha256': 'b' * 64}], False))
    except YandexDiskError as exc:
        assert 'другой файл' in str(exc)
    else:
        raise AssertionError('accepted a different file at the target')
    # A copy that does not match the original is an integrity error.
    try:
        asyncio.run(run([missing, {'type': 'file', 'size': 2048, 'sha256': 'c' * 64}], True))
    except YandexDiskError as exc:
        assert 'не совпадает' in str(exc)
    else:
        raise AssertionError('accepted a mismatching copy')
