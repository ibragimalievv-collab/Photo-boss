"""Accepted sale originals share one gallery, with current payment/access gates."""
import asyncio
import hashlib
import io
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from sqlalchemy import delete, func, select
from test_miniapp_release import TOKEN, signed
from test_workflow_media_preparation import setup

from app.miniapp_api import install_miniapp
from app.models import (
    DeliveryGallery,
    DeliveryPhoto,
    OperationRequest,
    Receipt,
    Sale,
    SaleDraft,
    SaleDraftPhoto,
    Setting,
    User,
    UserRole,
)
from app.services.receipts import refresh_payment_statuses
from app.services.sale_gallery import link_photo
from app.services.sale_workflow import (
    capture_draft_receipt,
    set_sale_counts,
    start_sale_draft,
)
from app.yandex_disk import ROOT, YandexDisk, YandexDiskError


def original(color):
    output = io.BytesIO()
    Image.new('RGB', (80, 60), color).save(output, format='JPEG')
    return output.getvalue()


async def fixture(tmp_path, monkeypatch, *, sold=2):
    monkeypatch.setenv('YANDEX_DISK_TOKEN', 'synthetic-account-one')
    engine, factory, work, packet = await setup(tmp_path/'sale.sqlite', 'sale_selected')
    did = packet['data']['draft']['id']
    async with factory() as session:
        session.add(User(id=4, tg_id=4, name='Synthetic owner'))
        await session.flush()
        session.add(UserRole(user_id=4, role='OWNER'))
        draft = await session.get(SaleDraft, did)
        await set_sale_counts(session, await session.get(User, 1), draft, 10, sold)
        bid = draft.booking_id
        await session.commit()
    bot = SimpleNamespace(token=TOKEN, me=AsyncMock(return_value=SimpleNamespace(username='SyntheticBot')),
                          send_message=AsyncMock(), send_document=AsyncMock(), send_photo=AsyncMock())
    work.api.bot = bot
    app = web.Application(client_max_size=25*1024*1024)
    api = install_miniapp(app, engine=engine, bot=bot, lessons=[])
    api.today = lambda: date(2026, 9, 22)
    client = TestClient(TestServer(app))
    await client.start_server()
    files, dirs, writes = {}, set(), []
    dirs.add(('synthetic-account-one', ROOT))

    async def metadata(disk, path):
        raw = files.get((disk.token, path))
        if raw is None:
            raise YandexDiskError('Yandex.Disk HTTP 404', http_status=404)
        return {'type': 'file', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

    async def ensure(disk, path):
        dirs.add((disk.token, path))

    async def put(disk, path, raw, **_kwargs):
        if not state.parallel:
            assert engine.pool.checkedout() == 0
        if (disk.token, path.rsplit('/', 1)[0]) not in dirs:
            raise YandexDiskError('Yandex.Disk HTTP 409', http_status=409)
        writes.append((disk.token, path))
        files[disk.token, path] = raw

    async def get(disk, path, **_kwargs):
        await metadata(disk, path)
        return files[disk.token, path]

    monkeypatch.setattr(YandexDisk, 'metadata', metadata)
    monkeypatch.setattr(YandexDisk, 'ensure_dir', ensure)
    monkeypatch.setattr(YandexDisk, 'upload_bytes', put)
    monkeypatch.setattr(YandexDisk, 'download_bytes', get)
    state = SimpleNamespace(engine=engine, factory=factory, work=work, client=client, bot=bot,
                            did=did, bid=bid, files=files, writes=writes, parallel=False)

    async def upload(color, *, key=None):
        body = packet | {'key': key or 'sale-photo-test-' + color,
                         'data': {'draft': {'id': state.did}}}
        return json.loads((await work.execute({'id': 1, 'roles': ['MANAGER']}, body, original(color))).text)

    async def complete(*, key='sale-complete-request-0001'):
        body = packet | {'key': key, 'kind': 'sale_complete',
                         'data': {'draft': {'id': state.did}, 'price': '400.00'}}
        return json.loads((await work.execute({'id': 1, 'roles': ['MANAGER']}, body)).text)

    async def pay(approved=True):
        async with factory() as session:
            receipts = list(await session.scalars(select(Receipt).where(Receipt.booking_id == bid)))
            for receipt in receipts:
                receipt.status = 'APPROVED' if approved else 'REJECTED'
                receipt.verified_amount = receipt.expected_amount
            await refresh_payment_statuses(session, bid)
            await session.commit()

    async def gallery():
        async with factory() as session:
            return await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == bid))

    state.upload, state.complete, state.pay, state.gallery = upload, complete, pay, gallery
    return state


async def close(f):
    await f.client.close()
    await f.engine.dispose()


def test_partial_sale_visible_to_staff_then_payment_releases_same_link_and_replay(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch)
        try:
            first = await f.upload('blue')
            g = await f.gallery()
            token = g.access_token
            async with f.factory() as session:
                photo = await session.get(SaleDraftPhoto, first['photoId'])
                pid = photo.delivery_photo_id
                assert pid is not None and await session.scalar(select(func.count(Sale.id))) == 0
            assert (await f.client.get(f'/api/miniapp/delivery/{f.bid}/photos/{pid}',
                headers={'X-Telegram-Init-Data': signed(2)})).status == 200
            assert (await f.client.get('/g/' + token + '/data')).status == 404
            last = await f.upload('green')
            assert last['gallery']['state'] == 'payment_pending' and last['gallery']['photoCount'] == 2
            assert not last['gallery']['ready'] and last['gallery']['clientUrl'] is None
            before = await f.complete()
            await f.pay()
            after = await f.complete()
            assert after['saleId'] == before['saleId'] and after['gallery']['ready']
            assert after['gallery']['clientUrl'].endswith('/g/' + token)
            assert after['gallery']['saleId'] == after['saleId']
            # Seller manager gets only this sale's QR; another manager cannot.
            qr = '/api/miniapp' + after['gallery']['qrPath']
            assert (await f.client.get(qr, headers={'X-Telegram-Init-Data': signed(1)})).status == 200
            assert (await f.client.get(qr, headers={'X-Telegram-Init-Data': signed(3)})).status == 403
            data = await (await f.client.get('/g/' + token + '/data')).json()
            assert len(data['photos']) == 2
            assert (await f.gallery()).access_token == token
            assert len(f.writes) == 2 and f.bot.send_message.await_count == 0
            async with f.factory() as session:
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(DeliveryPhoto.id))) == 2
                assert all('/Photo#2/' in path and f'/Продажа-{f.did}/' in path for _token, path in f.writes)
        finally:
            await close(f)
    asyncio.run(run())


def test_refund_revokes_direct_photo_cover_data_and_zip_before_delivery(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch, sold=1)
        try:
            await f.upload('blue')
            await f.pay()
            result = await f.complete()
            base = '/g/' + (await f.gallery()).access_token
            data = await (await f.client.get(base + '/data')).json()
            pid = data['photos'][0]['id']
            assert (await f.client.get(base + '/photos/' + str(pid))).status == 200
            await f.pay(False)
            for path in (base, base+'/data', base+f'/photos/{pid}', base+f'/photos/{pid}?preview=1', base+'/album.zip'):
                assert (await f.client.get(path)).status == 404
            replay = await f.complete()
            assert replay['saleId'] == result['saleId'] and replay['gallery']['state'] == 'payment_pending'
        finally:
            await close(f)
    asyncio.run(run())


def test_manual_close_is_not_reopened_by_payment_retry_or_sync(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch, sold=1)
        try:
            await f.upload('blue')
            token = (await f.gallery()).access_token
            response = await f.client.post(f'/api/miniapp/delivery/{f.bid}',
                headers={'X-Telegram-Init-Data': signed(4)}, json={'published': False})
            assert response.status == 200
            await f.pay()
            result = await f.complete()
            assert result['gallery']['state'] == 'closed' and not result['gallery']['ready']
            sync = await f.client.post('/api/miniapp' + result['gallery']['syncPath'],
                headers={'X-Telegram-Init-Data': signed(1)}, json={})
            assert sync.status == 200 and (await sync.json())['gallery']['state'] == 'closed'
            assert (await f.gallery()).access_token == token
            assert (await f.client.get('/g/' + token)).status == 404
        finally:
            await close(f)
    asyncio.run(run())


def test_pending_sale_duplicate_does_not_hide_existing_manual_selected_original(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch)
        try:
            raw = original('blue')
            path = ROOT + f'/delivery/booking-{f.bid}/legacy/Все фото/manual.jpg'
            f.files['synthetic-account-one', path] = raw
            async with f.factory() as session:
                g = DeliveryGallery(booking_id=f.bid, access_token='m'*32, title='Existing',
                    created_by_id=2, published=True, auto_publish=False, delivery_mode='SELECTED')
                session.add(g)
                await session.flush()
                photo = DeliveryPhoto(gallery_id=g.id, uploaded_by_id=2, filename='manual.jpg',
                    disk_path=path, sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw),
                    selected=True, upload_set='ALL')
                session.add(photo)
                await session.flush()
                manual_id = photo.id
                await session.commit()
            await f.upload('blue')
            public = await (await f.client.get('/g/'+'m'*32+'/data')).json()
            assert [p['id'] for p in public['photos']] == [manual_id]
            assert (await f.client.get('/g/'+'m'*32+'/album.zip')).status == 200
        finally:
            await close(f)
    asyncio.run(run())


def test_storage_change_requires_verified_sync_without_second_sale_or_path_guess(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch, sold=1)
        try:
            await f.upload('blue')
            await f.pay()
            ready = await f.complete()
            g = await f.gallery()
            async with f.factory() as session:
                before = await session.scalar(select(DeliveryPhoto))
                before_id, before_path = before.id, before.disk_path
            monkeypatch.setenv('YANDEX_DISK_TOKEN', 'synthetic-account-two')
            stale = await f.complete()
            assert stale['gallery']['state'] == 'storage_pending' and not stale['gallery']['ready']
            endpoint = '/api/miniapp' + stale['gallery']['syncPath']
            headers = {'X-Telegram-Init-Data': signed(1)}
            assert (await f.client.post(endpoint, headers=headers, json={})).status == 503
            assert (await f.client.get('/g/' + g.access_token)).status == 404
            f.files['synthetic-account-two', before_path] = original('blue')
            sync = await f.client.post(endpoint, headers=headers, json={})
            assert sync.status == 200 and (await sync.json())['gallery']['ready']
            async with f.factory() as session:
                after = await session.scalar(select(DeliveryPhoto))
                assert (after.id, after.disk_path) == (before_id, before_path)
                assert await session.scalar(select(func.count(Sale.id))) == 1
            assert (await f.complete())['saleId'] == ready['saleId']
        finally:
            await close(f)
    asyncio.run(run())


def test_bridge_failure_rolls_sale_photo_and_ack_back_then_reuses_original(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch, sold=1)
        from app.services import sale_gallery
        saved = sale_gallery.link_draft
        try:
            monkeypatch.setattr(sale_gallery, 'link_draft', AsyncMock(side_effect=ValueError('Synthetic bridge failure')))
            with pytest.raises(ValueError, match='Synthetic'):
                await f.upload('blue')
            async with f.factory() as session:
                for model in (Sale, SaleDraftPhoto, DeliveryPhoto, OperationRequest):
                    assert await session.scalar(select(func.count()).select_from(model)) == 0
            assert len(f.writes) == 1
            monkeypatch.setattr(sale_gallery, 'link_draft', saved)
            result = await f.upload('blue')
            assert result['saleId'] and len(f.writes) == 1
        finally:
            await close(f)
    asyncio.run(run())


def test_cancelled_draft_and_purged_gallery_cannot_be_revived_by_bridge(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch)
        try:
            first = await f.upload('blue')
            async with f.factory() as session:
                draft = await session.get(SaleDraft, f.did)
                photo = await session.get(SaleDraftPhoto, first['photoId'])
                draft.status = 'CANCELLED'
                with pytest.raises(ValueError, match='отменена'):
                    await link_photo(session, draft, photo, verified=True)
                await session.commit()
            with pytest.raises(ValueError):
                await f.upload('green')
            async with f.factory() as session:
                draft = await start_sale_draft(session, await session.get(User, 1), {'MANAGER'}, f.bid)
                await capture_draft_receipt(session, await session.get(User, 1), draft,
                    'another', 'another', 'another', {'status': 'not_configured'})
                await set_sale_counts(session, await session.get(User, 1), draft, 10, 2)
                f.did = draft.id
                await session.commit()
            await f.upload('blue', key='new-draft-same-original-0001')
            async with f.factory() as session:
                assert await session.scalar(select(func.count(DeliveryPhoto.id))) == 1
                session.add(Setting(key=f'gallery:meta:{f.bid}', value='{"purged":true}'))
                await session.execute(delete(DeliveryPhoto))
                await session.commit()
            endpoint = f'/api/miniapp/sale-drafts/{f.did}/gallery/sync'
            denied = await f.client.post(endpoint, headers={'X-Telegram-Init-Data': signed(1)}, json={})
            assert denied.status == 409
            async with f.factory() as session:
                assert await session.scalar(select(func.count(DeliveryPhoto.id))) == 0
                assert await session.scalar(select(func.count(Sale.id))) == 0
        finally:
            await close(f)
    asyncio.run(run())


def test_unpaid_followup_stays_private_and_old_ack_reports_its_own_sale(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch, sold=1)
        try:
            await f.upload('blue')
            await f.pay()
            first_draft = f.did
            first = await f.complete()
            base = '/g/' + (await f.gallery()).access_token
            async with f.factory() as session:
                draft = await start_sale_draft(session, await session.get(User, 1), {'MANAGER'}, f.bid)
                await capture_draft_receipt(session, await session.get(User, 1), draft,
                    'followup-receipt', 'followup-unique', 'followup-hash', {'status': 'not_configured'})
                await set_sale_counts(session, await session.get(User, 1), draft, 10, 1)
                f.did = draft.id
                await session.commit()
            followup = await f.upload('green')
            assert followup['gallery']['saleId'] != first['gallery']['saleId']
            assert followup['gallery']['state'] == 'payment_pending'
            async with f.factory() as session:
                p = await session.get(SaleDraftPhoto, followup['photoId'])
                unpaid_photo = p.delivery_photo_id
            data = await (await f.client.get(base+'/data')).json()
            assert len(data['photos']) == 1 and data['photos'][0]['id'] != unpaid_photo
            assert (await f.client.get(base+f'/photos/{unpaid_photo}')).status == 404
            f.did = first_draft
            replay = await f.complete()
            assert replay['gallery']['saleId'] == first['saleId'] and replay['gallery']['ready']
            assert replay['gallery']['photoCount'] == 1
            workflow = await (await f.client.get('/api/miniapp/workflow',
                headers={'X-Telegram-Init-Data': signed(1)})).json()
            booking = next(b for b in workflow['bookings'] if b['id'] == f.bid)
            assert booking['photographerId'] == 2 and booking['photographer'] == 'Photo'
            assert booking['gallery']['saleId'] == followup['saleId']
            assert booking['gallery']['state'] == 'payment_pending'
        finally:
            await close(f)
    asyncio.run(run())


def test_concurrent_final_upload_replay_creates_only_one_sale_and_link(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch, sold=1)
        try:
            # Another operation may briefly use the pool while this one writes;
            # per-request release is covered by the serial provider assertions.
            f.parallel = True
            a, b = await asyncio.gather(f.upload('blue'), f.upload('blue'))
            assert a['saleId'] == b['saleId'] and a['gallery'] == b['gallery']
            async with f.factory() as session:
                for model in (Sale, SaleDraftPhoto, DeliveryPhoto, OperationRequest):
                    assert await session.scalar(select(func.count()).select_from(model)) == 1
        finally:
            await close(f)
    asyncio.run(run())


def test_sync_rechecks_generation_and_seller_after_storage_io(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch, sold=1)
        try:
            await f.upload('blue')
            await f.pay()
            result = await f.complete()
            endpoint = '/api/miniapp' + result['gallery']['syncPath']
            async def changed(disk, path, **kwargs):
                assert f.engine.pool.checkedout() == 0
                async with f.factory() as session:
                    session.add(Setting(key='sales:generation', value='synthetic-next-generation'))
                    await session.commit()
                return f.files[disk.token, path]
            monkeypatch.setattr(YandexDisk, 'download_bytes', changed)
            response = await f.client.post(endpoint, headers={'X-Telegram-Init-Data': signed(1)}, json={})
            assert response.status == 409
            monkeypatch.setattr(YandexDisk, 'download_bytes', AsyncMock(return_value=original('blue')))
            async with f.factory() as session:
                await session.execute(delete(UserRole).where(UserRole.user_id == 1))
                await session.commit()
            assert (await f.client.post(endpoint, headers={'X-Telegram-Init-Data': signed(1)}, json={})).status == 403
            assert (await f.client.post(endpoint, headers={'X-Telegram-Init-Data': signed(3)}, json={})).status == 403
        finally:
            await close(f)
    asyncio.run(run())


def test_legacy_disk_reference_is_downloadable_and_keeps_three_provider_read_limit(tmp_path, monkeypatch):
    async def run():
        from app.disk_sale_media import REFERENCE, Reader
        f = await fixture(tmp_path, monkeypatch, sold=1)
        raw = original('blue')
        rid = '42424242-4242-4242-4242-424242424242'
        active = maximum = 0
        async def metadata(_reader, _path):
            return {'type': 'file', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        async def download(_reader, _path, **_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(.04)
                return raw
            finally:
                active -= 1
        monkeypatch.setattr(Reader, 'metadata', metadata)
        monkeypatch.setattr(Reader, 'download', download)
        try:
            async with f.factory() as session:
                draft = await session.get(SaleDraft, f.did)
                session.add(Setting(key=REFERENCE+rid, value=json.dumps({
                    'bookingId': f.bid, 'source': {'kind': 'public', 'root': '/',
                        'publicKey': 'https://disk.yandex.ru/d/synthetic-ref'},
                    'path': '/legacy.jpg', 'sha256': hashlib.sha256(raw).hexdigest(),
                    'bytes': len(raw)})))
                photo = SaleDraftPhoto(draft_id=f.did, telegram_file_id='disk-ref:'+rid,
                    telegram_unique_id='legacy-reference', storage_path='disk-ref:'+rid,
                    sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
                session.add(photo)
                await session.flush()
                await link_photo(session, draft, photo, verified=True)
                pid = photo.delivery_photo_id
                await session.commit()
            await f.complete()
            await f.pay()
            base = '/g/' + (await f.gallery()).access_token
            response = await f.client.get(base+f'/photos/{pid}')
            assert response.status == 200 and await response.read() == raw
            async with f.factory() as session:
                linked = await session.get(DeliveryPhoto, pid)
            maximum = 0
            # SQLite's reset barrier serializes HTTP requests; exercise the
            # production provider-read semaphore directly with detached rows.
            delivery = f.client.server.app['delivery']
            originals = await asyncio.gather(*(delivery.bytes(linked) for _ in range(5)))
            assert originals == [raw]*5
            assert maximum == 3 and active == 0
        finally:
            await close(f)
    asyncio.run(run())


def test_payment_revoked_while_original_loads_is_rechecked_before_response(tmp_path, monkeypatch):
    async def run():
        f = await fixture(tmp_path, monkeypatch, sold=1)
        try:
            await f.upload('blue')
            await f.pay()
            await f.complete()
            base = '/g/' + (await f.gallery()).access_token
            data = await (await f.client.get(base+'/data')).json()
            pid = data['photos'][0]['id']
            async def refund(disk, path, **_kwargs):
                assert f.engine.pool.checkedout() == 0
                await f.pay(False)
                return f.files[disk.token, path]
            monkeypatch.setattr(YandexDisk, 'download_bytes', refund)
            assert (await f.client.get(base+f'/photos/{pid}')).status == 404
        finally:
            await close(f)
    asyncio.run(run())
