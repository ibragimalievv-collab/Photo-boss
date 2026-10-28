"""Hierarchy follows the shoot, remains private, and never overwrites another guest."""
import asyncio
import hashlib
from datetime import date, time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery import ACTORS, image, setup

from app.gallery_folders import (
    booking_storage_root,
    directory_chain,
    folder_path,
    storage_folder,
)
from app.miniapp_security import AccessError
from app.models import Booking, Client, DeliveryPhoto, Hotel, User
from app.yandex_disk import ROOT, safe_path


async def hierarchy_fixture(monkeypatch):
    engine, delivery, client, bot = await setup(monkeypatch)
    async with AsyncSession(engine) as session:
        (await session.get(User, 2)).name = 'Анна Фотограф'
        (await session.get(User, 3)).name = 'Борис Фотограф'
        (await session.get(Hotel, 1)).name = 'Мира'
        (await session.get(Client, 1)).name = 'Семья Ивановых'
        session.add_all([Hotel(id=2, name='Мира'), Hotel(id=3, name=''),
                         Client(id=2, name='Петровы'), Client(id=3, name='')])
        await session.flush()
        b = await session.get(Booking, 1)
        b.shoot_date, b.shoot_time = date(2026, 10, 2), time(12)
        for bid, hotel, guest, photographer, day in (
            (2, 1, 2, 3, date(2026, 10, 2)),
            (3, 1, 1, 2, date(2026, 11, 10)),
            (4, 1, 1, 2, date(2026, 10, 2)),
            (5, 2, 2, 2, date(2026, 10, 2)),
            (6, 3, 3, None, date(2026, 12, 1)),
        ):
            session.add(Booking(id=bid, hotel_id=hotel, client_id=guest, package_id=1,
                room=str(200+bid), manager_id=5, photographer_id=photographer,
                shoot_date=day, shoot_time=time(12), status='ASSIGNED'))
        await session.commit()
    return engine, delivery, client, bot


def test_project_tree_uses_real_assignment_and_server_scopes(monkeypatch):
    async def run():
        engine, _delivery, client, bot = await hierarchy_fixture(monkeypatch)
        try:
            owner = await (await client.get('/api/miniapp/galleries', headers={'X-Test-Actor': '1'})).json()
            rows = {r['id']: r for r in owner['items']}
            assert rows[1]['folderPath'] == [
                {'key': 'hotel:1', 'label': 'Мира', 'sort': 'мира'},
                {'key': '2026-10', 'label': 'Октябрь 2026', 'sort': '2026-10'},
                {'key': '2026-10-02', 'label': '2', 'sort': '2026-10-02'},
                {'key': 'photographer:2', 'label': 'Анна Фотограф', 'sort': 'анна фотограф'}]
            assert rows[2]['folderPath'][-1]['label'] == 'Борис Фотограф'
            assert rows[3]['folderPath'][1]['label'] == 'Ноябрь 2026'
            assert rows[5]['folderPath'][0]['key'] != rows[1]['folderPath'][0]['key']
            assert rows[5]['folderPath'][0]['label'] == rows[1]['folderPath'][0]['label']
            assert rows[6]['folderPath'][0]['label'] == 'Отель не указан'
            assert rows[6]['folderPath'][-1]['label'] == 'Фотограф не указан'
            assert rows[6]['photographerId'] is None
            assert rows[1]['client'] == 'Семья Ивановых' and rows[1]['room'] == '200'
            assert rows[1]['photographerId'] == 2 and rows[1]['photographerId'] != 1
            for actor, allowed in ((2, {1, 3, 4, 5}), (3, {2}), (4, {1, 2, 3, 4}), (5, set())):
                data = await (await client.get('/api/miniapp/galleries',
                    headers={'X-Test-Actor': str(actor)})).json()
                assert {r['id'] for r in data['items']} == allowed
            assert bot.send_message.await_count == 0
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_new_uploads_have_hierarchy_unique_bookings_and_replay_keeps_original_paths(monkeypatch):
    stored, directories = {}, []
    monkeypatch.setattr('app.delivery.configured_from_env', lambda: ('synthetic-only', ''))

    async def ensure(_self, path):
        safe_path(path)
        directories.append(path)

    async def upload(_self, path, raw, **_kwargs):
        safe_path(path)
        stored[path] = raw

    async def metadata(_self, path):
        raw = stored[path]
        return {'type': 'file', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

    monkeypatch.setattr('app.delivery.YandexDisk.ensure_dir', ensure)
    monkeypatch.setattr('app.delivery.YandexDisk.upload_bytes', upload)
    monkeypatch.setattr('app.delivery.YandexDisk.metadata', metadata)

    async def run():
        engine, delivery, client, _ = await hierarchy_fixture(monkeypatch)
        try:
            saved = {}
            for bid in (1, 2, 3, 4, 5, 6):
                data = {'booking': bid, 'filename': 'original.png'}
                prepared = await delivery.prepare_photo(ACTORS[1], data, image())
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    first = await delivery.apply(session, ACTORS[1], 'delivery_photo', data, image(), prepared=prepared)
                    await session.commit()
                    saved[bid] = (await session.get(DeliveryPhoto, first['photoId'])).disk_path
                again = await delivery.prepare_photo(ACTORS[1], data, image())
                async with AsyncSession(engine) as session:
                    replay = await delivery.apply(session, ACTORS[1], 'delivery_photo', data, image(), prepared=again)
                    await session.commit()
                    assert replay['photoId'] == first['photoId'] and replay['duplicate']
            assert len(set(saved.values())) == len(stored) == 6
            assert '/Мира#1/Октябрь_2026/02/Анна_Фотограф#2/' in saved[1]
            assert '/Борис_Фотограф#3/' in saved[2]
            assert '/Ноябрь_2026/10/' in saved[3]
            assert saved[1] != saved[4] and '_booking-4/Все фото/' in saved[4]
            assert '/Мира#2/' in saved[5]
            assert '/Фотограф_не_указан#unknown/' in saved[6]
            for bid, path in saved.items():
                root = booking_storage_root(path, bid)
                assert root.endswith(f'_booking-{bid}')
                assert all(parent in directories for parent in directory_chain(root))
                with pytest.raises(ValueError):
                    booking_storage_root(path, bid+100)
            # Replaying a valid existing upload does not migrate it to the new tree.
            legacy = ROOT + '/delivery/booking-1/legacy/Все фото/original.png'
            async with AsyncSession(engine) as session:
                photo = await session.scalar(select(DeliveryPhoto).where(DeliveryPhoto.disk_path == saved[1]))
                photo.disk_path = legacy
                stored[legacy] = image()
                await session.commit()
            old = await delivery.prepare_photo(ACTORS[1], {'booking': 1, 'filename': 'original.png'}, image())
            assert old['path'] == legacy
            assert len(stored) == 7
            # New grouping metadata never leaks through the guest capability.
            async with AsyncSession(engine) as session:
                gallery = await delivery.ensure(session, ACTORS[1], await session.get(Booking, 1))
                gallery.published = True
                token = gallery.access_token
                await session.commit()
            public = await (await client.get('/g/' + token + '/data')).json()
            assert 'folderPath' not in public and len(public['photos']) == 1
            assert (await client.get('/g/' + token + '/photos/2')).status == 404
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['hotel', 'photographer_name', 'room'])
def test_folder_changes_during_upload_reject_attachment(monkeypatch, change):
    monkeypatch.setattr('app.delivery.configured_from_env', lambda: ('synthetic-only', ''))
    monkeypatch.setattr('app.delivery.YandexDisk.ensure_dir', AsyncMock())
    monkeypatch.setattr('app.delivery.YandexDisk.upload_bytes', AsyncMock())

    async def run():
        engine, delivery, client, _ = await hierarchy_fixture(monkeypatch)
        try:
            data = {'booking': 1, 'filename': 'original.png'}
            prepared = await delivery.prepare_photo(ACTORS[1], data, image())
            async with AsyncSession(engine) as session:
                if change == 'hotel':
                    (await session.get(Hotel, 1)).name = 'Другой отель'
                elif change == 'photographer_name':
                    (await session.get(User, 2)).name = 'Другое имя'
                else:
                    (await session.get(Booking, 1)).room = '999'
                await session.commit()
            async with AsyncSession(engine) as session:
                with pytest.raises(AccessError) as denied:
                    await delivery.apply(session, ACTORS[1], 'delivery_photo', data, image(), prepared=prepared)
                assert denied.value.status == 409
                await session.rollback()
            async with AsyncSession(engine) as session:
                assert list(await session.scalars(select(DeliveryPhoto))) == []
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_missing_dates_safe_names_and_separate_years():
    booking = SimpleNamespace(id=77, hotel_id=None, photographer_id=None,
        shoot_date=None, shoot_time=None, room='')
    assert [p['label'] for p in folder_path(booking, None, None)] == [
        'Отель не указан', 'Месяц не указан', 'Число не указано', 'Фотограф не указан']
    safe_path(storage_folder(booking, '../Guest/../../', '../Hotel', '../Photo'))
    booking.shoot_date = date(2025, 10, 2)
    earlier = folder_path(booking, 'Отель', 'Фотограф')[1]
    booking.shoot_date = date(2026, 10, 2)
    later = folder_path(booking, 'Отель', 'Фотограф')[1]
    assert earlier['key'] != later['key'] and earlier['sort'] < later['sort']


def test_explicit_purge_uses_only_this_shoot_leaf_and_rejects_other_booking(monkeypatch):
    from app import yandex_disk
    monkeypatch.setattr(yandex_disk, 'configured_from_env', lambda: ('synthetic-only', ''))
    remove = AsyncMock()
    monkeypatch.setattr(yandex_disk.YandexDisk, 'delete_confirmed', remove)

    async def run():
        engine, delivery, client, _ = await hierarchy_fixture(monkeypatch)
        try:
            paths = {}
            async with AsyncSession(engine) as session:
                for bid in (1, 4):
                    b = await session.get(Booking, bid)
                    guest = await session.get(Client, b.client_id)
                    _context, (folder, all_folder, _selected) = await delivery.photo_location(session, b, guest)
                    paths[bid] = folder
                    gallery = await delivery.ensure(session, ACTORS[1], b)
                    session.add(DeliveryPhoto(gallery_id=gallery.id, uploaded_by_id=2,
                        filename='original.png', disk_path=all_folder + '/original.png',
                        sha256='a'*64, byte_size=100, upload_set='ALL'))
                await session.commit()
            headers = {'X-Test-Actor': '1'}
            await client.post('/api/miniapp/galleries/1/preferences', headers=headers, json={'trashed': True})
            response = await client.post('/api/miniapp/galleries/1/purge', headers=headers, json={'confirm': True})
            assert response.status == 200
            remove.assert_awaited_once_with(paths[1])
            async with AsyncSession(engine) as session:
                remaining = list(await session.scalars(select(DeliveryPhoto)))
                assert len(remaining) == 1 and remaining[0].disk_path.startswith(paths[4] + '/')
                foreign = await delivery.ensure(session, ACTORS[1], await session.get(Booking, 6))
                session.add(DeliveryPhoto(gallery_id=foreign.id, uploaded_by_id=2,
                    filename='foreign.png', disk_path=remaining[0].disk_path,
                    sha256='b'*64, byte_size=100, upload_set='ALL'))
                await session.commit()
            await client.post('/api/miniapp/galleries/6/preferences', headers=headers, json={'trashed': True})
            denied = await client.post('/api/miniapp/galleries/6/purge', headers=headers, json={'confirm': True})
            assert denied.status == 409
            assert remove.await_count == 1
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())
