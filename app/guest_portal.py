"""Global guests and private family slideshow jobs; never grants a staff role."""
import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
from datetime import timedelta
from pathlib import Path

from aiohttp import web
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import family_video
from .miniapp_security import AccessError, validate_init_data
from .models import (
    DeliveryClaim,
    DeliveryContact,
    DeliveryGallery,
    DeliveryPhoto,
    GuestAccount,
    GuestAlbum,
    GuestFilm,
    GuestSession,
    utc_now,
)
from .password_login import (
    normalize_login,
    password_hash,
    password_matches,
    password_value,
)
from .yandex_disk import ROOT, YandexDisk, configured_from_env

logger = logging.getLogger(__name__)
COOKIE = '__Host-pb_guest'
PREFIX = '/api/guest'
MAX_AUDIO = 12 * 1024 * 1024


def stamp(gallery):
    return hashlib.sha256((gallery.access_token + ':' + (gallery.password_hash or '')).encode()).hexdigest()


def song_draft(names):
    family = names or 'Наша семья'
    return f'{family} — вместе каждый день,\nСвет наших встреч хранит семейный альбом.\nПусть остаётся радость этих мгновений,\nМы нашу историю вместе поём.'


class GuestPortal:
    def __init__(self, api, delivery):
        self.api, self.delivery = api, delivery
        self.hash_slots = asyncio.Semaphore(2)
        self.dummy = password_hash(secrets.token_urlsafe(32))
        self.wake = asyncio.Event()

    def guard(self, request):
        if (request.headers.get('Origin') != self.delivery.origin()
                or request.headers.get('X-PhotoBoss-Guest') != '1'
                or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
            raise AccessError('Откройте личный кабинет Photo Boss.', 403)

    async def body(self, request):
        if request.content_length and request.content_length > 8192:
            raise AccessError('Слишком большой запрос.', 413)
        raw = bytearray()
        async for chunk in request.content.iter_chunked(8193):
            raw.extend(chunk)
            if len(raw) > 8192:
                raise AccessError('Слишком большой запрос.', 413)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError
            return value
        except (ValueError, TypeError, UnicodeError):
            raise AccessError('Некорректный запрос.', 400) from None

    async def hash_work(self, function, *args):
        async with self.hash_slots:
            return await asyncio.to_thread(function, *args)

    async def rate(self, request):
        bucket = int(utc_now().timestamp()) // 300
        peer = hashlib.sha256((request.remote or 'unknown').encode()).hexdigest()
        key = f'guest:auth-rate:{bucket:010d}:{peer}'
        async with self.api.engine.begin() as conn:
            count = await conn.scalar(text("INSERT INTO settings(key,value) VALUES (:key,'1') "
                'ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(settings.value AS INTEGER)+1 AS TEXT) RETURNING value'), {'key': key})
            await conn.execute(text('DELETE FROM settings WHERE key LIKE :prefix AND key < :cutoff'),
                {'prefix': 'guest:auth-rate:%', 'cutoff': f'guest:auth-rate:{bucket-1:010d}:'})
        if int(count) > 20:
            raise AccessError('Повторите вход через пять минут.', 429)

    async def account(self, session, request):
        raw = request.cookies.get(COOKIE, '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
            raise AccessError('Войдите в личный кабинет.', 401)
        saved = await session.get(GuestSession, hashlib.sha256(raw.encode()).hexdigest())
        guest = await session.get(GuestAccount, saved.guest_id) if saved and saved.expires_at > utc_now() else None
        if not guest or not guest.active:
            raise AccessError('Войдите в личный кабинет.', 401)
        return guest

    async def issue(self, session, guest):
        raw = secrets.token_urlsafe(32)
        await session.execute(delete(GuestSession).where(GuestSession.expires_at <= utc_now()))
        # Retain at most five remembered devices per guest.
        old = (await session.scalars(select(GuestSession).where(GuestSession.guest_id == guest.id)
                    .order_by(GuestSession.expires_at.desc()))).all()
        for item in old[4:]:
            await session.delete(item)
        session.add(GuestSession(token_hash=hashlib.sha256(raw.encode()).hexdigest(), guest_id=guest.id,
                                 expires_at=utc_now() + timedelta(days=30)))
        return raw

    @staticmethod
    def cookie(response, raw):
        response.set_cookie(COOKIE, raw, secure=True, httponly=True, samesite='Strict', path='/', max_age=30*86400)

    async def sync_albums(self, session, guest):
        galleries = (await session.scalars(select(DeliveryClaim.gallery_id).join(DeliveryContact,
                    DeliveryClaim.contact_id == DeliveryContact.id).where(DeliveryContact.tg_id == guest.tg_id))).all()
        for gid in galleries:
            if not await session.get(GuestAlbum, (guest.id, gid)):
                try:
                    async with session.begin_nested():
                        session.add(GuestAlbum(guest_id=guest.id, gallery_id=gid))
                        await session.flush()
                except IntegrityError:
                    pass  # Concurrent entry of the same verified guest.

    async def session_open(self, request):
        await self.rate(request)
        if await self.body(request):
            raise AccessError('Личность определяется сервером.', 400)
        tg_id = validate_init_data(request.headers.get('X-Telegram-Init-Data', ''), self.api.bot.token, max_age=600)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            guest = await session.scalar(select(GuestAccount).where(GuestAccount.tg_id == tg_id).with_for_update())
            contact = await session.scalar(select(DeliveryContact).where(DeliveryContact.tg_id == tg_id))
            if not guest:
                try:
                    async with session.begin_nested():
                        guest = GuestAccount(tg_id=tg_id, name=contact.name if contact else 'Гость')
                        session.add(guest)
                        await session.flush()
                except IntegrityError:
                    guest = await session.scalar(select(GuestAccount).where(GuestAccount.tg_id == tg_id).with_for_update())
            if not guest.active:
                raise AccessError('Доступ к аккаунту отключён.', 403)
            await self.sync_albums(session, guest)
            raw = await self.issue(session, guest)
            await session.commit()
        response = web.json_response({'authenticated': True})
        self.cookie(response, raw)
        return response

    async def login(self, request):
        await self.rate(request)
        body = await self.body(request)
        if set(body) != {'login', 'password'}:
            raise AccessError('Введите логин и пароль.', 400)
        login = normalize_login(body['login'])
        password = password_value(body['password'])
        # The login is the confirmed Telegram ID, never an arbitrary submitted identity.
        tg_id = int(login) if login.isascii() and login.isdigit() and len(login) <= 16 else 0
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            guest = await session.scalar(select(GuestAccount).where(GuestAccount.tg_id == tg_id).with_for_update())
            valid = await self.hash_work(password_matches, password, guest.password_hash if guest and guest.password_hash else self.dummy)
            if not guest or not guest.active or not guest.password_hash or not valid:
                raise AccessError('Неверный логин или пароль.', 401)
            raw = await self.issue(session, guest)
            await session.commit()
        response = web.json_response({'authenticated': True})
        self.cookie(response, raw)
        return response

    async def set_password(self, request):
        await self.rate(request)
        body = await self.body(request)
        if set(body) - {'password', 'currentPassword'}:
            raise AccessError('Некорректные параметры пароля.', 400)
        password = password_value(body.get('password'))
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            guest = await self.account(session, request)
            guest = await session.get(GuestAccount, guest.id, with_for_update=True)
            init = request.headers.get('X-Telegram-Init-Data', '')
            try:
                verified = bool(init and validate_init_data(init, self.api.bot.token, max_age=600) == guest.tg_id)
            except AccessError:
                verified = False
            current = body.get('currentPassword', '')
            if not verified and not (guest.password_hash and isinstance(current, str) and len(current) <= 128
                    and await self.hash_work(password_matches, current, guest.password_hash)):
                raise AccessError('Подтвердите аккаунт через Telegram или введите текущий пароль.', 403)
            guest.password_hash = await self.hash_work(password_hash, password)
            await session.execute(delete(GuestSession).where(GuestSession.guest_id == guest.id))
            raw = await self.issue(session, guest)
            await session.commit()
        response = web.json_response({'saved': True})
        self.cookie(response, raw)
        return response

    async def logout(self, request):
        async with AsyncSession(self.api.engine) as session:
            raw = request.cookies.get(COOKIE, '')
            await session.execute(delete(GuestSession).where(GuestSession.token_hash == hashlib.sha256(raw.encode()).hexdigest()))
            await session.commit()
        response = web.json_response({'loggedOut': True})
        response.del_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='Strict')
        return response

    async def gallery(self, session, guest, gid, *, unlocked=True):
        grant = await session.get(GuestAlbum, (guest.id, int(gid)))
        gallery = await session.get(DeliveryGallery, int(gid)) if grant else None
        if not gallery or not gallery.published or (gallery.expires_at and gallery.expires_at <= utc_now()):
            raise AccessError('Альбом недоступен.', 404)
        if unlocked and gallery.password_hash and grant.unlock_stamp != stamp(gallery):
            raise AccessError('Введите пароль альбома.', 403)
        return gallery

    async def me(self, request):
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            guest = await self.account(session, request)
            await self.sync_albums(session, guest)
            albums = []
            for gid in (await session.scalars(select(GuestAlbum.gallery_id).where(GuestAlbum.guest_id == guest.id))).all():
                try:
                    g = await self.gallery(session, guest, gid, unlocked=False)
                    grant = await session.get(GuestAlbum, (guest.id, gid))
                    locked = bool(g.password_hash and grant.unlock_stamp != stamp(g))
                    albums.append({'id': g.id, 'title': g.title, 'locked': locked})
                except AccessError:
                    continue
            await session.commit()
            return web.json_response({'name': guest.name, 'login': str(guest.tg_id),
                'passwordConfigured': bool(guest.password_hash), 'albums': albums,
                'videoAvailable': family_video.available(), 'aiMusicAvailable': False})

    async def unlock(self, request):
        await self.rate(request)
        body = await self.body(request)
        value = body.get('password', '')
        if set(body) != {'password'} or not isinstance(value, str) or len(value) > 100:
            raise AccessError('Введите пароль альбома.', 400)
        async with AsyncSession(self.api.engine) as session:
            guest = await self.account(session, request)
            g = await self.gallery(session, guest, request.match_info['gallery'], unlocked=False)
            from .delivery import password_matches as album_password_matches
            if g.password_hash and not await self.hash_work(album_password_matches, value, g.password_hash):
                raise AccessError('Неверный пароль альбома.', 401)
            grant = await session.get(GuestAlbum, (guest.id, g.id))
            grant.unlock_stamp = stamp(g)
            await session.commit()
        return web.json_response({'unlocked': True})

    async def photos(self, request):
        async with AsyncSession(self.api.engine) as session:
            guest = await self.account(session, request)
            g = await self.gallery(session, guest, request.match_info['gallery'])
            rows = (await session.scalars(self.delivery.client_photos_query(g).order_by(DeliveryPhoto.id))).all()
            base = PREFIX + f'/galleries/{g.id}/photos/'
            return web.json_response({'photos': [{'id': p.id, 'name': p.filename, 'url': base+str(p.id)} for p in rows]})

    async def photo(self, request):
        async with AsyncSession(self.api.engine) as session:
            guest = await self.account(session, request)
            g = await self.gallery(session, guest, request.match_info['gallery'])
            p = await session.scalar(self.delivery.client_photos_query(g).where(DeliveryPhoto.id == int(request.match_info['photo'])))
            if not p:
                raise AccessError('Фото недоступно.', 404)
            raw = await self.delivery.bytes(p)
            from .services.photo_storage import image_format
            if request.query.get('preview') == '1':
                raw = await asyncio.to_thread(self.delivery.preview, raw)
            return web.Response(body=raw, content_type=image_format(raw)[1])

    def storage(self):
        token, client_id = configured_from_env()
        if not token:
            raise AccessError('Хранилище временно недоступно.', 503)
        return YandexDisk(token, client_id)

    async def film(self, session, guest, fid, *, lock=False):
        film = await session.get(GuestFilm, int(fid), with_for_update=lock)
        if not film or film.guest_id != guest.id:
            raise AccessError('Ролик не найден.', 404)
        return film

    @staticmethod
    def describe(film):
        return {'id': film.id, 'title': film.title, 'status': film.status, 'error': film.error,
                'lyrics': film.lyrics, 'format': film.format, 'hasAudio': bool(film.audio_path),
                'url': PREFIX+f'/films/{film.id}/video' if film.status == 'READY' else None}

    async def films(self, request):
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            guest = await self.account(session, request)
            if request.method == 'GET':
                rows = (await session.scalars(select(GuestFilm).where(GuestFilm.guest_id == guest.id)
                                             .order_by(GuestFilm.id.desc()).limit(50))).all()
                return web.json_response({'films': [self.describe(f) for f in rows]})
            body = await self.body(request)
            if set(body) != {'requestKey', 'galleryId', 'photoIds', 'title', 'names', 'story', 'format'}:
                raise AccessError('Некорректные параметры ролика.', 400)
            key = body['requestKey']
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', key):
                raise AccessError('Некорректный номер запроса.', 400)
            # Serialize concurrent requests and limits per global guest.
            guest = await session.get(GuestAccount, guest.id, with_for_update=True)
            old = await session.scalar(select(GuestFilm).where(GuestFilm.guest_id == guest.id, GuestFilm.request_key == key))
            if old:
                if (old.gallery_id != body['galleryId'] or json.loads(old.photo_ids) != body['photoIds']
                        or old.title != body['title'] or old.family_names != body['names']
                        or old.family_story != body['story'] or old.format != body['format']):
                    raise AccessError('Номер запроса уже использован для другого ролика.', 409)
                return web.json_response(self.describe(old))
            if type(body['galleryId']) is not int:
                raise AccessError('Некорректный альбом.', 400)
            ids = body['photoIds']
            if (not isinstance(ids, list) or not 2 <= len(ids) <= 12 or any(type(i) is not int for i in ids)
                    or len(set(ids)) != len(ids) or not isinstance(body['format'], str)
                    or body['format'] not in {'VERTICAL', 'HORIZONTAL'}):
                raise AccessError('Выберите от 2 до 12 разных фотографий и формат.', 400)
            for name, maximum in [('title', 100), ('names', 300), ('story', 1000)]:
                if not isinstance(body[name], str) or len(body[name]) > maximum:
                    raise AccessError('Слишком длинное описание.', 400)
            if not body['title'].strip():
                raise AccessError('Укажите название ролика.', 400)
            g = await self.gallery(session, guest, body['galleryId'])
            allowed = set((await session.scalars(self.delivery.client_photos_query(g).with_only_columns(DeliveryPhoto.id))).all())
            if not set(ids) <= allowed:
                raise AccessError('В ролик можно добавить только разрешённые вам фотографии.', 403)
            count = await session.scalar(select(func.count(GuestFilm.id)).where(GuestFilm.guest_id == guest.id,
                                           GuestFilm.created_at >= utc_now()-timedelta(days=1)))
            if count >= 5:
                raise AccessError('На сегодня лимит: пять семейных роликов.', 429)
            film = GuestFilm(guest_id=guest.id, request_key=key, gallery_id=g.id, photo_ids=json.dumps(ids),
                title=body['title'], family_names=body['names'], family_story=body['story'],
                lyrics=song_draft(body['names']), format=body['format'])
            session.add(film)
            await session.commit()
            return web.json_response(self.describe(film), status=201)

    async def audio(self, request):
        if not family_video.available():
            raise AccessError('Создание видео пока недоступно на сервере.', 503)
        async with AsyncSession(self.api.engine) as session:
            guest = await self.account(session, request)
            film = await self.film(session, guest, request.match_info['film'], lock=True)
            await self.gallery(session, guest, film.gallery_id)
            if film.status != 'DRAFT':
                raise AccessError('Музыку можно добавить до создания ролика.', 409)
            raw = bytearray()
            async for chunk in request.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > MAX_AUDIO:
                    raise AccessError('Музыка: до 12 МБ.', 413)
            try:
                await family_video.validate_audio(raw)
            except ValueError as exc:
                raise AccessError(str(exc), 400) from exc
            storage = self.storage()
            folder = ROOT + f'/guest-media/guest-{guest.id}'
            for path in (ROOT, ROOT+'/guest-media', folder):
                await storage.ensure_dir(path)
            path = folder + f'/audio-{film.id}-{secrets.token_hex(8)}'
            await storage.upload_bytes(path, bytes(raw), content_type='application/octet-stream')
            film.audio_path = path
            await session.commit()
            return web.json_response({'saved': True})

    async def queue(self, request):
        if not family_video.available():
            raise AccessError('Создание видео пока недоступно на сервере.', 503)
        self.storage()  # Fail before acknowledging a job when storage is unconfigured.
        body = await self.body(request)
        if set(body) != {'lyrics', 'confirmed'} or body['confirmed'] is not True or not isinstance(body['lyrics'], str) or len(body['lyrics']) > 2000:
            raise AccessError('Проверьте имена и подтвердите создание ролика.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            guest = await self.account(session, request)
            film = await self.film(session, guest, request.match_info['film'], lock=True)
            await self.gallery(session, guest, film.gallery_id)
            if film.status == 'DRAFT':
                film.lyrics, film.status = body['lyrics'], 'QUEUED'
                await session.commit()
            elif film.status not in {'QUEUED', 'RENDERING', 'READY'}:
                raise AccessError('Создайте новый ролик.', 409)
        self.wake.set()
        return web.json_response(self.describe(film), status=202)

    async def cancel(self, request):
        async with AsyncSession(self.api.engine) as session:
            guest = await self.account(session, request)
            film = await self.film(session, guest, request.match_info['film'], lock=True)
            if film.status == 'READY':
                raise AccessError('Готовый ролик уже создан.', 409)
            film.status = 'CANCELLED'
            film.run_token = None
            await session.commit()
        return web.json_response({'cancelled': True})

    async def authorized_photos(self, session, film):
        guest = await session.get(GuestAccount, film.guest_id)
        if not guest or not guest.active:
            raise AccessError('Аккаунт недоступен.', 403)
        g = await self.gallery(session, guest, film.gallery_id)
        ids = json.loads(film.photo_ids)
        photos = (await session.scalars(self.delivery.client_photos_query(g).where(DeliveryPhoto.id.in_(ids)))).all()
        mapping = {p.id: p for p in photos}
        if not set(ids) <= mapping.keys():
            raise AccessError('Доступ к фотографиям изменён.', 403)
        return [mapping[i] for i in ids]

    async def video(self, request):
        async with AsyncSession(self.api.engine) as session:
            guest = await self.account(session, request)
            film = await self.film(session, guest, request.match_info['film'])
            await self.authorized_photos(session, film)
            if film.status != 'READY' or not film.video_path:
                raise AccessError('Ролик пока не готов.', 409)
            async with self.delivery.download_slots:
                raw = await self.storage().download_bytes(film.video_path, max_bytes=40*1024*1024)
            headers = {'Accept-Ranges': 'bytes'}
            if request.query.get('download') == '1':
                headers['Content-Disposition'] = f'attachment; filename="Photo-Boss-Family-{film.id}.mp4"'
            length, status = len(raw), 200
            requested = request.headers.get('Range')
            if requested:
                match = re.fullmatch(r'bytes=(\d{0,10})-(\d{0,10})', requested)
                if not match or not any(match.groups()):
                    return web.Response(status=416, headers={**headers, 'Content-Range': f'bytes */{length}'})
                first, last = match.groups()
                if first:
                    start, end = int(first), min(int(last), length-1) if last else length-1
                else:
                    start, end = max(0, length-int(last)), length-1
                if start >= length or end < start:
                    return web.Response(status=416, headers={**headers, 'Content-Range': f'bytes */{length}'})
                headers['Content-Range'] = f'bytes {start}-{end}/{length}'
                raw, status = raw[start:end+1], 206
            return web.Response(body=raw, content_type='video/mp4', status=status, headers=headers)

    async def run_once(self):
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            # Recover expired leases after crashes without publishing stale workers' output.
            await session.execute(update(GuestFilm).where(GuestFilm.status == 'RENDERING',
                GuestFilm.started_at < utc_now()-timedelta(minutes=15)).values(status='QUEUED', run_token=None))
            fid = await session.scalar(select(GuestFilm.id).where(GuestFilm.status == 'QUEUED').order_by(GuestFilm.id).limit(1))
            if not fid:
                await session.commit()
                return False
            lease = secrets.token_hex(16)
            changed = await session.execute(update(GuestFilm).where(GuestFilm.id == fid, GuestFilm.status == 'QUEUED')
                .values(status='RENDERING', started_at=utc_now(), run_token=lease, error=None))
            await session.commit()
            if not changed.rowcount:
                return True
        status, error, path = 'READY', None, None
        try:
            async with asyncio.timeout(600):
                async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
                    film = await session.get(GuestFilm, fid)
                    photos = await self.authorized_photos(session, film)
                    raws = []
                    for photo in photos:
                        raws.append(await asyncio.to_thread(self.delivery.preview, await self.delivery.bytes(photo)))
                    audio = await self.storage().download_bytes(film.audio_path) if film.audio_path else None
                    video = await family_video.render(raws, format=film.format, audio=audio)
                    # A cancelled job or revoked photo is never delivered.
                    await session.refresh(film)
                    if film.status != 'RENDERING' or film.run_token != lease:
                        return True
                    await self.authorized_photos(session, film)
                    folder = ROOT + f'/guest-media/guest-{film.guest_id}'
                    for directory in (ROOT, ROOT+'/guest-media', folder):
                        await self.storage().ensure_dir(directory)
                    path = folder + f'/film-{fid}-{lease}.mp4'
                    await self.storage().upload_bytes(path, video, content_type='video/mp4')
        except Exception:
            logger.warning('Family film %s failed', fid, exc_info=True)
            status, error = 'FAILED', 'Не удалось создать ролик. Проверьте доступ к фото и попробуйте новый заказ.'
        async with AsyncSession(self.api.engine) as session:
            await session.execute(update(GuestFilm).where(GuestFilm.id == fid, GuestFilm.status == 'RENDERING', GuestFilm.run_token == lease)
                                  .values(status=status, error=error, video_path=path))
            await session.commit()
        return True

    async def worker(self):
        while True:
            try:
                if await self.run_once():
                    continue
            except Exception:
                logger.warning('Family film queue temporarily unavailable', exc_info=True)
            self.wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=10)

    async def page(self, request):
        return web.FileResponse(Path(__file__).parent / 'webapp' / 'guest.html')

    async def config(self, request):
        username = (await self.api.bot.me()).username
        if not username or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            raise AccessError('Регистрация временно недоступна.', 503)
        return web.json_response({'telegramUrl': f'https://t.me/{username}?start=guest'})

    @web.middleware
    async def middleware(self, request, handler):
        if not (request.path.startswith(PREFIX+'/') or request.path.startswith('/guest/')):
            return await handler(request)
        try:
            if request.method not in {'GET', 'HEAD'}:
                self.guard(request)
            response = await handler(request)
        except AccessError as exc:
            response = web.json_response({'error': str(exc)}, status=exc.status)
        except web.HTTPException as exc:
            response = web.json_response({'error': 'Недопустимый запрос.'}, status=exc.status)
        except Exception:
            logger.warning('Guest request failed', exc_info=True)
            response = web.json_response({'error': 'Сервис временно недоступен.'}, status=503)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy':
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self'; "
            "media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"})
        return response


def install_guest_portal(app, api, delivery):
    service = GuestPortal(api, delivery)
    app['guest_portal'] = service
    app.middlewares.append(service.middleware)
    app.router.add_get('/guest/', service.page)
    for method, path, handler in [('GET', '/config', service.config), ('POST', '/session', service.session_open), ('POST', '/login', service.login),
        ('POST', '/password', service.set_password), ('POST', '/logout', service.logout), ('GET', '/me', service.me),
        ('GET', r'/galleries/{gallery:\d+}/photos', service.photos),
        ('POST', r'/galleries/{gallery:\d+}/unlock', service.unlock),
        ('GET', r'/galleries/{gallery:\d+}/photos/{photo:\d+}', service.photo),
        ('GET', '/films', service.films), ('POST', '/films', service.films),
        ('POST', r'/films/{film:\d+}/audio', service.audio), ('POST', r'/films/{film:\d+}/render', service.queue),
        ('POST', r'/films/{film:\d+}/cancel', service.cancel), ('GET', r'/films/{film:\d+}/video', service.video)]:
        app.router.add_route(method, PREFIX+path, handler)
    async def lifetime(application):
        task = asyncio.create_task(service.worker())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    app.cleanup_ctx.append(lifetime)
    return service
