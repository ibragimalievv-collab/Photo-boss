"""Private global guest accounts and film jobs against disposable data/storage."""
import asyncio
import hashlib
import hmac
import io
import json
import secrets
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from aiohttp import TCPConnector, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import family_video
from app.companies import install_companies
from app.db import Base
from app.delivery import Delivery
from app.delivery import password_hash as album_hash
from app.guest_portal import COOKIE, GuestPortal, install_guest_portal
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Client,
    DeliveryClaim,
    DeliveryContact,
    DeliveryGallery,
    DeliveryPhoto,
    GuestAccount,
    GuestSession,
    Hotel,
    Package,
    User,
    UserRole,
    utc_now,
)

TOKEN = '123456:fake-guest-only-test-token'
ORIGIN = 'https://photos.example'
HEADERS = {'Origin': ORIGIN, 'X-PhotoBoss-Guest': '1'}


def signed(tg_id, age=0):
    values = {'auth_date': str(int(time.time())-age), 'user': json.dumps({'id': tg_id})}
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    values['hash'] = hmac.new(secret, '\n'.join(f'{k}={values[k]}' for k in sorted(values)).encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def image():
    output = io.BytesIO()
    Image.new('RGB', (40, 30), 'orange').save(output, format='PNG')
    return output.getvalue()


async def setup(monkeypatch, *, ssl_context=None):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        session.add_all([User(id=1, tg_id=1, name='Owner'), Hotel(id=1, name='Hotel'),
            Client(id=1, name='Guest'), Package(id=1, name='Photos', price_per_photo=400),
            DeliveryContact(id=1, tg_id=100001, name='Guest one'), DeliveryContact(id=2, tg_id=100002, name='Guest two')])
        await session.flush()
        for i in (1, 2):
            session.add(Booking(id=i, hotel_id=1, client_id=1, package_id=1, room='1', manager_id=1,
                photographer_id=1, shoot_date=utc_now().date(), shoot_time=utc_now().time(), status='ASSIGNED'))
        await session.flush()
        for i in (1, 2):
            session.add(DeliveryGallery(id=i, booking_id=i, access_token=secrets.token_urlsafe(24), title=f'Album {i}',
                published=True, delivery_mode='SELECTED', created_by_id=1))
        await session.flush()
        for i in range(1, 5):
            session.add(DeliveryPhoto(id=i, gallery_id=1 if i < 4 else 2, uploaded_by_id=1, filename=f'{i}.png',
                disk_path=f'app:/photos/{i}', sha256=str(i)*64, byte_size=len(image()), selected=i < 3))
        session.add_all([DeliveryClaim(gallery_id=1, contact_id=1), DeliveryClaim(gallery_id=2, contact_id=2)])
        await session.commit()
    bot = SimpleNamespace(token=TOKEN, me=AsyncMock(return_value=SimpleNamespace(username='PhotoBossTest')))
    async def body(request):
        return await request.json()
    api = SimpleNamespace(engine=engine, bot=bot, body=body)
    delivery = Delivery(api)
    monkeypatch.setattr(delivery, 'origin', lambda: ORIGIN)
    monkeypatch.setattr(delivery, 'bytes', AsyncMock(return_value=image()))
    @web.middleware
    async def staff(request, handler):
        if request.path.startswith('/api/miniapp/'):
            request['miniapp_actor'] = {'id': 1, 'roles': [request.headers.get('X-Test-Role', 'ADMIN')]}
        try:
            return await handler(request)
        except AccessError as exc:
            return web.json_response({'error': str(exc)}, status=exc.status)
    app = web.Application(middlewares=[staff], client_max_size=25*1024*1024)
    svc = install_guest_portal(app, api, delivery)
    # Queue tests advance the worker explicitly to avoid racing HTTP assertions.
    app.cleanup_ctx.clear()
    install_companies(app, api)
    async def asset(request):
        name = request.match_info['asset']
        if name not in {'js/guest.js', 'css/guest.css'}:
            raise web.HTTPNotFound()
        return web.FileResponse(Path(__file__).resolve().parents[1]/'app'/'webapp'/name)
    app.router.add_get('/app/{asset:.*}', asset)
    files = {}
    async def upload(path, raw, **kwargs):
        files[path] = raw
    async def download(path, **kwargs):
        return files[path]
    storage = SimpleNamespace(ensure_dir=AsyncMock(), upload_bytes=upload, download_bytes=download)
    monkeypatch.setattr(svc, 'storage', lambda: storage)
    server = TestServer(app)
    if ssl_context:
        await server.start_server(ssl=ssl_context)
    client = TestClient(server, connector=TCPConnector(ssl=False))
    await client.start_server()
    return engine, svc, client, files


async def enter(client, tg_id=100001):
    response = await client.post('/api/guest/session', headers={**HEADERS, 'X-Telegram-Init-Data': signed(tg_id)}, json={})
    assert response.status == 200, await response.text()
    return {**HEADERS, 'Cookie': COOKIE+'='+response.cookies[COOKIE].value}


def film_body(**changes):
    return {'requestKey': 'first-family-film-request', 'galleryId': 1, 'photoIds': [2, 1], 'title': 'Family',
            'names': 'Sample family', 'story': 'Holiday', 'format': 'VERTICAL', **changes}


def test_verified_global_identity_no_staff_roles_and_album_isolation(monkeypatch):
    async def run():
        engine, _svc, client, _ = await setup(monkeypatch)
        try:
            assert (await client.post('/api/guest/session', headers=HEADERS, json={'userId': 100001})).status == 400
            assert (await client.post('/api/guest/session', headers=HEADERS, json={})).status == 401
            assert (await client.post('/api/guest/session', headers={**HEADERS, 'X-Telegram-Init-Data': signed(100001, 601)}, json={})).status == 401
            assert (await client.post('/api/guest/session', headers={**HEADERS, 'Origin': 'https://evil.example'}, json={})).status == 403
            headers = await enter(client)
            other = await enter(client, 100002)
            me = await (await client.get('/api/guest/me', headers=headers)).json()
            assert [a['id'] for a in me['albums']] == [1]
            assert (await client.get('/api/guest/galleries/2/photos', headers=headers)).status == 404
            assert (await client.get('/api/guest/galleries/1/photos/4', headers=headers)).status == 404
            assert (await client.get('/api/guest/galleries/1/photos/3', headers=headers)).status == 404
            assert (await client.get('/api/guest/galleries/1/photos', headers=other)).status == 404
            assert [p['id'] for p in (await (await client.get('/api/guest/galleries/1/photos', headers=headers)).json())['photos']] == [1, 2]
            await enter(client)
            async with AsyncSession(engine) as session:
                assert await session.scalar(select(func.count(GuestAccount.id))) == 2
                assert await session.scalar(select(func.count(User.id))) == 1
                assert await session.scalar(select(func.count(UserRole.id))) == 0
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_password_reset_revokes_devices_logout_and_missing_login(monkeypatch):
    async def run():
        engine, _svc, client, _ = await setup(monkeypatch)
        try:
            headers = await enter(client)
            assert (await client.post('/api/guest/password', headers=headers, json={'password': 'long-password-123'})).status == 403
            assert (await client.post('/api/guest/password', headers={**headers, 'X-Telegram-Init-Data': signed(100001)}, json={'password': 'long-password-123'})).status == 200
            assert (await client.get('/api/guest/me', headers=headers)).status == 401
            assert (await client.post('/api/guest/login', headers=HEADERS, json={'login': '100002', 'password': 'long-password-123'})).status == 401
            response = await client.post('/api/guest/login', headers=HEADERS, json={'login': '100001', 'password': 'long-password-123'})
            assert response.status == 200
            headers = {**HEADERS, 'Cookie': COOKIE+'='+response.cookies[COOKIE].value}
            assert response.cookies[COOKIE]['secure'] and response.cookies[COOKIE]['httponly']
            assert (await client.get('/api/guest/me', headers=headers)).status == 200
            assert (await client.post('/api/guest/password', headers={**headers, 'X-Telegram-Init-Data': signed(100001, 700)},
                       json={'currentPassword': 'long-password-123', 'password': 'next-long-password'})).status == 200
            response = await client.post('/api/guest/login', headers=HEADERS, json={'login': '100001', 'password': 'next-long-password'})
            headers = {**HEADERS, 'Cookie': COOKIE+'='+response.cookies[COOKIE].value}
            assert (await client.post('/api/guest/logout', headers=headers, json={})).status == 200
            assert (await client.get('/api/guest/me', headers=headers)).status == 401
            async with AsyncSession(engine) as session:
                assert await session.scalar(select(func.count(GuestSession.token_hash))) == 1
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_album_password_expiry_and_changed_password_revoke_unlock(monkeypatch):
    async def run():
        engine, _svc, client, _ = await setup(monkeypatch)
        try:
            headers = await enter(client)
            async with AsyncSession(engine) as session:
                await session.execute(update(DeliveryGallery).where(DeliveryGallery.id == 1).values(password_hash=album_hash('album-pass')))
                await session.commit()
            assert (await client.get('/api/guest/galleries/1/photos', headers=headers)).status == 403
            assert (await client.post('/api/guest/galleries/1/unlock', headers=headers, json={'password': 'wrong'})).status == 401
            assert (await client.post('/api/guest/galleries/1/unlock', headers=headers, json={'password': 'album-pass'})).status == 200
            assert (await client.get('/api/guest/galleries/1/photos', headers=headers)).status == 200
            async with AsyncSession(engine) as session:
                await session.execute(update(DeliveryGallery).where(DeliveryGallery.id == 1).values(password_hash=album_hash('new-pass')))
                await session.commit()
            assert (await client.get('/api/guest/galleries/1/photos', headers=headers)).status == 403
            async with AsyncSession(engine) as session:
                await session.execute(update(DeliveryGallery).where(DeliveryGallery.id == 1).values(expires_at=utc_now()-timedelta(seconds=1)))
                await session.commit()
            assert (await client.get('/api/guest/galleries/1/photos', headers=headers)).status == 404
            assert (await (await client.get('/api/guest/me', headers=headers)).json())['albums'] == []
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_film_replay_validation_cancel_worker_and_photo_revocation(monkeypatch):
    async def run():
        engine, svc, client, files = await setup(monkeypatch)
        monkeypatch.setattr(family_video, 'available', lambda: True)
        renderer = AsyncMock(return_value=b'fake-mp4')
        monkeypatch.setattr(family_video, 'render', renderer)
        try:
            headers, other = await enter(client), await enter(client, 100002)
            for changes, status in [({'photoIds': [1, 3]}, 403), ({'photoIds': [1, 4]}, 403),
                ({'photoIds': [1, 1]}, 400), ({'photoIds': [True, 2]}, 400), ({'format': []}, 400)]:
                assert (await client.post('/api/guest/films', headers=headers, json=film_body(**changes))).status == status
            response = await client.post('/api/guest/films', headers=headers, json=film_body())
            assert response.status == 201, await response.text()
            film = await response.json()
            assert (await (await client.post('/api/guest/films', headers=headers, json=film_body())).json())['id'] == film['id']
            assert (await client.post('/api/guest/films', headers=headers, json=film_body(title='Different'))).status == 409
            fid = film['id']
            assert (await client.post(f'/api/guest/films/{fid}/cancel', headers=other, json={})).status == 404
            assert (await client.post(f'/api/guest/films/{fid}/render', headers=headers, json={'lyrics': 'checked', 'confirmed': False})).status == 400
            assert (await client.post(f'/api/guest/films/{fid}/render', headers=headers, json={'lyrics': 'checked', 'confirmed': True})).status == 202
            # Reconstruct the service: queued state lives in DB, not Python memory.
            fresh = GuestPortal(svc.api, svc.delivery)
            monkeypatch.setattr(fresh, 'storage', svc.storage)
            assert await fresh.run_once()
            assert renderer.await_count == 1
            assert (await client.get(f'/api/guest/films/{fid}/video', headers=other)).status == 404
            assert await (await client.get(f'/api/guest/films/{fid}/video', headers=headers)).read() == b'fake-mp4'
            part = await client.get(f'/api/guest/films/{fid}/video', headers={**headers, 'Range': 'bytes=2-5'})
            assert part.status == 206 and await part.read() == b'ke-m'
            assert part.headers['Content-Range'] == 'bytes 2-5/8'
            assert (await client.get(f'/api/guest/films/{fid}/video', headers={**headers, 'Range': 'bytes=999-'})).status == 416
            async with AsyncSession(engine) as session:
                await session.execute(update(DeliveryPhoto).where(DeliveryPhoto.id == 1).values(selected=False))
                await session.commit()
            assert (await client.get(f'/api/guest/films/{fid}/video', headers=headers)).status == 403
            async with AsyncSession(engine) as session:
                await session.execute(update(DeliveryPhoto).where(DeliveryPhoto.id == 1).values(selected=True))
                await session.commit()
            response = await client.post('/api/guest/films', headers=headers, json=film_body(requestKey='second-family-film-request'))
            fid = (await response.json())['id']
            await client.post(f'/api/guest/films/{fid}/render', headers=headers, json={'lyrics': '', 'confirmed': True})
            await client.post(f'/api/guest/films/{fid}/cancel', headers=headers, json={})
            assert not await svc.run_once()
            assert renderer.await_count == 1
            assert len(files) == 1
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


def test_company_owner_only_mapping_and_disabling_preserves_guest(monkeypatch):
    async def run():
        engine, _svc, client, _ = await setup(monkeypatch)
        try:
            headers = await enter(client)
            body = {'name': 'Company', 'country': 'RU', 'timezone': 'Europe/Moscow', 'currency': 'RUB'}
            assert (await client.post('/api/miniapp/companies', json=body)).status == 403
            owner = {'X-Test-Role': 'OWNER'}
            first = await (await client.post('/api/miniapp/companies', headers=owner, json=body)).json()
            second = await (await client.post('/api/miniapp/companies', headers=owner, json=body)).json()
            assert first['monthlyPrice'] == 0 and first['staffOnboardingEnabled'] is False
            assert (await client.post(f"/api/miniapp/companies/{first['id']}/galleries", headers=owner, json={'galleryId': 1})).status == 200
            assert (await client.post(f"/api/miniapp/companies/{second['id']}/galleries", headers=owner, json={'galleryId': 1})).status == 409
            assert (await client.post(f"/api/miniapp/companies/{first['id']}", headers=owner, json={'active': False})).status == 200
            assert (await client.get('/api/guest/me', headers=headers)).status == 200
            assert (await client.get('/api/guest/galleries/1/photos', headers=headers)).status == 200
            async with AsyncSession(engine) as session:
                assert await session.scalar(select(func.count(GuestAccount.id))) == 1
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.skipif(not family_video.available(), reason='FFmpeg is not installed')
def test_real_mp4_dimensions_duration_audio_and_reject_invalid_music(tmp_path):
    async def run():
        with pytest.raises(ValueError):
            await family_video.validate_audio(b'invalid audio')
        music = tmp_path/'music.wav'
        await family_video.command('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', str(music))
        audio = music.read_bytes()
        await family_video.validate_audio(audio)
        result = await family_video.render([image(), image()], format='HORIZONTAL', audio=audio)
        path = tmp_path/'film.mp4'
        path.write_bytes(result)
        info = json.loads(await family_video.command('ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)))
        video = next(s for s in info['streams'] if s['codec_type'] == 'video')
        assert (video['width'], video['height']) == (1280, 720)
        assert video['codec_name'] == 'h264'
        assert any(s['codec_type'] == 'audio' and s['codec_name'] == 'aac' for s in info['streams'])
        assert 5.9 <= float(info['format']['duration']) <= 6.1
    asyncio.run(run())
