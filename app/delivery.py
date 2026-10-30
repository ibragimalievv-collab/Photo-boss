"""Client delivery uses a separate, explicitly published album, never all shoot originals."""
import asyncio
import hashlib
import hmac
import html
import io
import re
import secrets
import tempfile
import zipfile
from datetime import datetime, timedelta

from aiohttp import web
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .gallery_folders import directory_chain, selected_folder, storage_folder
from .miniapp_security import AccessError
from .models import (
    Booking,
    Client,
    DeliveryBookingResponse,
    DeliveryCampaign,
    DeliveryCampaignRecipient,
    DeliveryClaim,
    DeliveryContact,
    DeliveryGallery,
    DeliveryPhoto,
    Hotel,
    Sale,
    Shooting,
    User,
    UserRole,
    utc_now,
)
from .services.booking_access import booking_scope, can_access_booking
from .services.core import audit
from .services.photo_storage import image_format
from .services.sale_gallery import effective_published, guest_photo_clause
from .yandex_disk import (
    YandexDisk,
    YandexDiskError,
    configured_from_env,
    validate_saved,
)

SERVICES = {}


def can_edit(actor, booking):
    return bool({'OWNER', 'ADMIN'} & set(actor['roles'])) or (
        'PHOTOGRAPHER' in actor['roles'] and booking.photographer_id == actor['id'])


def can_view(actor, booking):
    # Managers can see the control queue for their records but not photographic work.
    return can_edit(actor, booking)


def password_hash(value):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ':' + digest.hex()


def password_matches(value, encoded):
    try:
        salt, digest = encoded.split(':')
        return hmac.compare_digest(hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex(), digest)
    except (ValueError, TypeError):
        return False


def archive_filename(photo):
    """Use a flat, portable ZIP member name, including for legacy stored names."""
    name = photo.filename.replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', '_', name).strip(' .')
    return f'{photo.id}-{name or "photo"}'


class Delivery:
    def __init__(self, api):
        self.api = api
        self.download_slots = asyncio.Semaphore(3)
        self.failures = {}

    def origin(self):
        from .launch_policy import app_url
        return app_url().split('/app/', 1)[0]

    async def booking(self, session, actor, bid, *, edit=False):
        try:
            bid = int(bid)
        except (TypeError, ValueError) as exc:
            raise AccessError('Некорректная съёмка.', 400) from exc
        b = await session.get(Booking, bid, with_for_update=edit)
        if not b:
            raise AccessError('Съёмка не найдена.', 404)
        if not await can_access_booking(session, actor['id'], actor['roles'], b, photos=True):
            raise AccessError('Нет доступа к фотографиям этой съёмки.', 403)
        return b

    async def ensure(self, session, actor, b):
        g = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == b.id))
        if not g:
            client = await session.get(Client, b.client_id)
            g = DeliveryGallery(booking_id=b.id, access_token=secrets.token_urlsafe(24),
                                title=(client.name if client else 'Ваши фотографии')[:120], created_by_id=actor['id'])
            session.add(g)
            await session.flush()
        return g

    @staticmethod
    def client_photos_query(gallery):
        query = select(DeliveryPhoto).where(DeliveryPhoto.gallery_id == gallery.id)
        if gallery.delivery_mode == 'ALL':
            return query.where(or_(DeliveryPhoto.upload_set == 'ALL', DeliveryPhoto.origin == 'SALE'),
                               guest_photo_clause())
        direct = aliased(DeliveryPhoto)
        uploaded_separately = select(direct.id).where(
            direct.gallery_id == DeliveryPhoto.gallery_id,
            direct.sha256 == DeliveryPhoto.sha256,
            direct.upload_set == 'SELECTED', guest_photo_clause(direct)).exists().correlate(DeliveryPhoto)
        return query.where(or_(DeliveryPhoto.upload_set == 'SELECTED',
            and_(DeliveryPhoto.selected.is_(True), ~uploaded_separately)), guest_photo_clause())

    async def describe(self, session, actor, b):
        g = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == b.id))
        photos = (await session.scalars(select(DeliveryPhoto).where(DeliveryPhoto.gallery_id == g.id).order_by(DeliveryPhoto.id))).all() if g else []
        user = await session.get(User, b.photographer_id) if b.photographer_id else None
        base = f'/api/miniapp/delivery/{b.id}'
        result = {'bookingId': b.id, 'photographer': user.name if user else None, 'canEdit': can_edit(actor, b),
                  'title': g.title if g else 'Ваши фотографии',
                  'published': await effective_published(session, g) if g else False,
                  'deliveryMode': (g.delivery_mode if g else 'ALL'),
                  'passwordEnabled': bool(g and g.password_hash), 'expiresAt': g.expires_at.isoformat() if g and g.expires_at else None,
                  'openedAt': g.opened_at.isoformat() if g and g.opened_at else None,
                  'downloadedAt': g.downloaded_at.isoformat() if g and g.downloaded_at else None,
                  'handedAt': g.handed_at.isoformat() if g and g.handed_at else None,
                  'photos': [{'id': p.id, 'name': p.filename, 'bytes': p.byte_size, 'selected': bool(p.selected), 'uploadSet': p.upload_set, 'digest': p.sha256,
                              'url': base + f'/photos/{p.id}'} for p in photos]}
        if g:
            claims = (await session.scalars(select(DeliveryClaim).where(DeliveryClaim.gallery_id == g.id))).all()
            result['botStartedAt'] = min((c.started_at for c in claims), default=None)
            result['botLinkSentAt'] = max((c.link_sent_at for c in claims if c.link_sent_at), default=None)
            result['botStartedAt'] = result['botStartedAt'].isoformat() if result['botStartedAt'] else None
            result['botLinkSentAt'] = result['botLinkSentAt'].isoformat() if result['botLinkSentAt'] else None
            result['clientUrl'] = self.origin() + '/g/' + g.access_token
            bot = await self.api.bot.me()
            result['botUrl'] = 'https://t.me/' + bot.username + '?start=' + ('gallery_' if g.published else 'booking_') + g.access_token
        from .gallery_workspace import GalleryWorkspace
        return await GalleryWorkspace(self).enrich(session, result, g)

    async def listing(self, request):
        a = request['miniapp_actor']
        async with AsyncSession(self.api.engine) as session:
            b = await self.booking(session, a, request.match_info['booking'])
            return web.json_response(await self.describe(session, a, b))

    async def configure(self, request):
        a = request['miniapp_actor']
        body = await self.api.body(request)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            b = await self.booking(session, a, request.match_info['booking'], edit=True)
            g = await self.ensure(session, a, b)
            allowed = {'title', 'published', 'password', 'expiresDays', 'rotate', 'deliveryMode'}
            if set(body) - allowed:
                raise AccessError('Неизвестная настройка галереи.', 400)
            if 'title' in body:
                if not isinstance(body['title'], str) or not 1 <= len(body['title'].strip()) <= 120:
                    raise AccessError('Название: от 1 до 120 символов.', 400)
                g.title = body['title'].strip()
            if 'password' in body:
                value = body['password']
                if not isinstance(value, str) or (value and not 6 <= len(value) <= 100):
                    raise AccessError('Пароль: от 6 до 100 символов.', 400)
                g.password_hash = await asyncio.to_thread(password_hash, value) if value else None
            if 'expiresDays' in body:
                days = body['expiresDays']
                if type(days) is not int or not 0 <= days <= 365:
                    raise AccessError('Срок доступа: от 0 до 365 дней.', 400)
                g.expires_at = utc_now() + timedelta(days=days) if days else None
            if 'deliveryMode' in body:
                mode = body['deliveryMode']
                if not isinstance(mode, str) or mode not in {'ALL', 'SELECTED'}:
                    raise AccessError('Выберите: все фото или выбранные.', 400)
                g.delivery_mode = mode
            if 'published' in body:
                if type(body['published']) is not bool:
                    raise AccessError('Неверный статус галереи.', 400)
                g.published = body['published']
                g.auto_publish = False
            from .gallery_workspace import read_setting
            meta = await read_setting(session, f'gallery:meta:{b.id}', {})
            if g.published and meta.get('trashed'):
                raise AccessError('Сначала восстановите проект из корзины.', 409)
            if g.published and {'published', 'deliveryMode'} & set(body):
                q = self.client_photos_query(g).with_only_columns(DeliveryPhoto.id)
                if not await session.scalar(q.limit(1)):
                    raise AccessError('Сначала загрузите фотографии для выбранного режима выдачи.', 409)
            if 'rotate' in body:
                if type(body['rotate']) is not bool:
                    raise AccessError('Некорректная настройка ссылки.', 400)
                if body['rotate']:
                    g.access_token = secrets.token_urlsafe(24)
            await audit(session, await session.get(User, a['id']), 'delivery_gallery_configured', 'booking', b.id)
            await session.commit()
            return web.json_response(await self.describe(session, a, b))

    @staticmethod
    def photo_identity(data, raw):
        return (data['booking'], data['filename'], data.get('photoSet', 'ALL'),
                data.get('selected', False), hashlib.sha256(raw).hexdigest())

    @staticmethod
    def photo_context(booking, client, hotel, photographer):
        return (booking.id, booking.client_id, booking.shoot_date.isoformat(),
                str(booking.shoot_time), client.name if client else None,
                booking.hotel_id, hotel.name if hotel else None,
                booking.photographer_id, photographer.name if photographer else None, booking.room)

    @staticmethod
    def photo_folders(booking, client, hotel, photographer):
        folder = storage_folder(booking, client.name if client else None,
                                hotel.name if hotel else None, photographer.name if photographer else None)
        return folder, folder + '/Все фото', folder + '/Выбранные'

    async def photo_location(self, session, booking, client):
        hotel = await session.get(Hotel, booking.hotel_id) if booking.hotel_id else None
        photographer = await session.get(User, booking.photographer_id) if booking.photographer_id else None
        return (self.photo_context(booking, client, hotel, photographer),
                self.photo_folders(booking, client, hotel, photographer))

    @staticmethod
    async def verify_photo(raw):
        # Reading a large image or waiting for a worker thread also belongs
        # outside the final database transaction.
        from PIL import Image
        def verify():
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 60_000_000:
                    raise ValueError('Изображение слишком большое.')
                image.verify()
        try:
            await asyncio.to_thread(verify)
        except Exception as exc:
            raise AccessError('Не удалось прочитать фотографию. Нужен JPEG или PNG.', 400) from exc

    async def prepare_photo(self, actor, data, raw):
        """Read-only preflight, then verify and upload without a DB lease."""
        if not ({'booking', 'filename'} <= set(data) <= {'booking', 'filename', 'selected', 'photoSet'}):
            raise AccessError('Некорректные поля выдачи.', 400)
        if type(data['booking']) is not int:
            raise AccessError('Некорректная съёмка.', 400)
        photo_set = data.get('photoSet', 'ALL')
        if not isinstance(photo_set, str) or photo_set not in {'ALL', 'SELECTED'} or ('selected' in data and type(data['selected']) is not bool) or ('photoSet' in data and 'selected' in data):
            raise AccessError('Выберите папку: все фотографии или выбранные фотографии.', 400)
        if not raw or len(raw) > 20 * 1024 * 1024:
            raise AccessError('Фото: не более 20 МБ.', 413)
        if not isinstance(data['filename'], str) or not data['filename'] or len(data['filename']) > 250:
            raise AccessError('Некорректное имя фото.', 400)
        ext, mime = image_format(raw)
        digest = hashlib.sha256(raw).hexdigest()
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            b = await self.booking(session, actor, data['booking'])
            g = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == b.id))
            from .gallery_workspace import read_setting
            meta = await read_setting(session, f'gallery:meta:{b.id}', {})
            if meta.get('purged') or meta.get('trashed'):
                raise AccessError('Проект удалён или находится в корзине. Загрузка недоступна.', 409)
            old = await session.scalar(select(DeliveryPhoto).where(DeliveryPhoto.gallery_id == g.id,
                DeliveryPhoto.sha256 == digest, DeliveryPhoto.upload_set == photo_set)) if g else None
            old_path = old.disk_path if old else None
            client = await session.get(Client, b.client_id)
            context, (folder, all_folder, chosen_folder) = await self.photo_location(session, b, client)
        await self.verify_photo(raw)
        token, client_id = configured_from_env()
        if not token:
            raise AccessError('Яндекс Диск недоступен. Фото остаётся в очереди.', 503)
        storage = YandexDisk(token, client_id)
        prepared = {'identity': self.photo_identity(data, raw), 'context': context}
        if old_path:
            try:
                validate_saved(await storage.metadata(old_path), raw)
                return prepared | {'path': old_path}
            except YandexDiskError as exc:
                if 'HTTP 404' not in str(exc) and 'не совпадает' not in str(exc):
                    raise
        for directory in (*directory_chain(folder), all_folder, chosen_folder):
            await storage.ensure_dir(directory)
        path = (chosen_folder if photo_set == 'SELECTED' else all_folder) + f'/{digest}.{ext}'
        await storage.upload_bytes(path, raw, content_type=mime)
        selected = photo_set == 'SELECTED' or data.get('selected', False)
        if selected and photo_set == 'ALL':
            await storage.upload_bytes(chosen_folder + f'/{digest}.{ext}', raw, content_type=mime)
        return prepared | {'path': path}

    async def apply(self, session, actor, kind, data, raw=None, *, prepared=None):
        photo_fields = {'booking', 'filename'} <= set(data) <= {'booking', 'filename', 'selected', 'photoSet'}
        if not (photo_fields if kind == 'delivery_photo' else set(data) == {'booking', 'delivered'}):
            raise AccessError('Некорректные поля выдачи.', 400)
        if type(data['booking']) is not int:
            raise AccessError('Некорректная съёмка.', 400)
        photo_set = data.get('photoSet', 'ALL')
        if not isinstance(photo_set, str) or photo_set not in {'ALL', 'SELECTED'} or ('selected' in data and type(data['selected']) is not bool) or ('photoSet' in data and 'selected' in data):
            raise AccessError('Выберите папку: все фотографии или выбранные фотографии.', 400)
        b = await self.booking(session, actor, data['booking'], edit=True)
        g = await self.ensure(session, actor, b)
        from .gallery_workspace import read_setting
        meta = await read_setting(session, f'gallery:meta:{b.id}', {})
        if meta.get('purged') or meta.get('trashed'):
            raise AccessError('Проект удалён или находится в корзине. Загрузка недоступна.', 409)
        if kind == 'delivery_handoff':
            if type(data['delivered']) is not bool:
                raise AccessError('Некорректная отметка выдачи.', 400)
            g.handed_at = utc_now() if data['delivered'] else None
            await audit(session, await session.get(User, actor['id']), 'delivery_handoff', 'booking', b.id)
            return {'bookingId': b.id, 'delivered': bool(g.handed_at)}
        if not raw or len(raw) > 20 * 1024 * 1024:
            raise AccessError('Фото: не более 20 МБ.', 413)
        name = data['filename']
        if not isinstance(name, str) or not name or len(name)>250:
            raise AccessError('Некорректное имя фото.', 400)
        ext, mime = image_format(raw)
        if prepared is None:
            await self.verify_photo(raw)
        elif prepared['identity'] != self.photo_identity(data, raw):
            raise AccessError('Подготовленное фото не соответствует операции.', 409)
        digest = hashlib.sha256(raw).hexdigest()
        old = await session.scalar(select(DeliveryPhoto).where(DeliveryPhoto.gallery_id == g.id, DeliveryPhoto.sha256 == digest, DeliveryPhoto.upload_set == photo_set))
        if prepared is not None:
            client = await session.get(Client, b.client_id)
            context, _folders = await self.photo_location(session, b, client)
            if prepared['context'] != context:
                raise AccessError('Данные съёмки изменились. Повторите загрузку.', 409)
            path = prepared['path']
            if old:
                old.disk_path, old.byte_size = path, len(raw)
                await session.flush()
                return {'bookingId': b.id, 'photoId': old.id, 'duplicate': True, 'storageVerified': True}
            name = re.sub(r'[^\w .()-]', '_', name)[:140] or 'photo.' + ext
            selected = photo_set == 'SELECTED' or data.get('selected', False)
            photo = DeliveryPhoto(gallery_id=g.id, uploaded_by_id=actor['id'], filename=name,
                disk_path=path, sha256=digest, byte_size=len(raw), selected=selected, upload_set=photo_set)
            session.add(photo)
            await session.flush()
            await audit(session, await session.get(User, actor['id']), 'delivery_photo_uploaded', 'booking', b.id)
            return {'bookingId': b.id, 'photoId': photo.id, 'selected': selected, 'uploadSet': photo_set}
        token, client_id = configured_from_env()
        if not token:
            raise AccessError('Яндекс Диск недоступен. Фото остаётся в очереди.', 503)
        storage = YandexDisk(token, client_id)
        if old:
            try:
                validate_saved(await storage.metadata(old.disk_path), raw)
                return {'bookingId': b.id, 'photoId': old.id, 'duplicate': True, 'storageVerified': True}
            except YandexDiskError as exc:
                if 'HTTP 404' not in str(exc) and 'не совпадает' not in str(exc):
                    raise
        client = await session.get(Client, b.client_id)
        _context, (folder, all_folder, chosen_folder) = await self.photo_location(session, b, client)
        for directory in (*directory_chain(folder), all_folder, chosen_folder):
            await storage.ensure_dir(directory)
        path = (chosen_folder if photo_set == 'SELECTED' else all_folder) + f'/{digest}.{ext}'
        await storage.upload_bytes(path, raw, content_type=mime)
        selected = photo_set == 'SELECTED' or data.get('selected', False)
        if selected and photo_set == 'ALL':
            await storage.upload_bytes(chosen_folder + f'/{digest}.{ext}', raw, content_type=mime)
        if old:
            # A DB duplicate can refer to an account that has been disconnected.
            # Re-upload the provided original to the current Disk before success.
            old.disk_path, old.byte_size = path, len(raw)
            await session.flush()
            return {'bookingId': b.id, 'photoId': old.id, 'duplicate': True, 'storageVerified': True}
        name = re.sub(r'[^\w .()-]', '_', name)[:140] or 'photo.' + ext
        photo = DeliveryPhoto(gallery_id=g.id, uploaded_by_id=actor['id'], filename=name,
                              disk_path=path, sha256=digest, byte_size=len(raw), selected=selected, upload_set=photo_set)
        session.add(photo)
        await session.flush()
        await audit(session, await session.get(User, actor['id']), 'delivery_photo_uploaded', 'booking', b.id)
        return {'bookingId': b.id, 'photoId': photo.id, 'selected': selected, 'uploadSet': photo_set}

    async def select_photo(self, request):
        actor = request['miniapp_actor']
        body = await self.api.body(request)
        if set(body) != {'selected'} or type(body['selected']) is not bool:
            raise AccessError('Некорректный выбор фотографии.', 400)
        from .gallery_selection_guard import selection_guard
        async with selection_guard(self.api.engine, int(request.match_info['booking'])):
            async def current(session):
                live_actor = await self.download_actor(session, request)
                booking = await self.booking(session, live_actor, request.match_info['booking'], edit=True)
                photo = await session.get(DeliveryPhoto, int(request.match_info['photo']))
                gallery = await session.get(DeliveryGallery, photo.gallery_id) if photo else None
                if not gallery or gallery.booking_id != booking.id:
                    raise AccessError('Фото не найдено.', 404)
                if photo.upload_set == 'SELECTED':
                    raise AccessError('Фотография загружена отдельно в папку «Выбранные».', 409)
                return booking, photo, gallery

            async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
                b, p, g = await current(session)
                if p.selected == body['selected']:
                    return web.json_response({'photoId': p.id, 'selected': p.selected})
                original = (p.disk_path, p.sha256)
            token, client_id = configured_from_env()
            if not token:
                raise AccessError('Яндекс Диск недоступен.', 503)
            storage = YandexDisk(token, client_id)
            raw = await self.bytes(p)
            try:
                chosen_folder = selected_folder(p.disk_path, b.id)
            except ValueError as exc:
                raise AccessError('Расположение исходного фото не подтверждено. Загрузите его повторно.', 409) from exc
            # Revalidate before provider writes, after the potentially slow read.
            async with AsyncSession(self.api.engine) as session:
                b, latest, g = await current(session)
                if (latest.disk_path, latest.sha256) != original:
                    raise AccessError('Фотография изменилась. Обновите галерею.', 409)
                independent = await session.scalar(select(DeliveryPhoto.id).where(
                    DeliveryPhoto.gallery_id == g.id, DeliveryPhoto.sha256 == p.sha256,
                    DeliveryPhoto.upload_set == 'SELECTED').limit(1))
            folder = chosen_folder.rsplit('/', 1)[0]
            for directory in (*directory_chain(folder), chosen_folder):
                await storage.ensure_dir(directory)
            ext, mime = image_format(raw)
            selected_path = chosen_folder + f'/{p.sha256}.{ext}'
            if body['selected']:
                await storage.upload_bytes(selected_path, raw, content_type=mime)
            elif not independent:
                await storage.delete(selected_path)
            # Never acknowledge a DB change based on stale permissions/metadata.
            async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
                b, p, g = await current(session)
                if (p.disk_path, p.sha256) != original:
                    raise AccessError('Фотография изменилась. Обновите галерею.', 409)
                p.selected = body['selected']
                await audit(session, await session.get(User, actor['id']), 'delivery_photo_selected', 'booking', b.id)
                await session.commit()
                return web.json_response({'photoId': p.id, 'selected': p.selected})

    async def staff_photo(self, request):
        async with AsyncSession(self.api.engine) as session:
            a = await self.download_actor(session, request)
            b = await self.booking(session, a, request.match_info['booking'])
            p = await session.get(DeliveryPhoto, int(request.match_info['photo']))
            g = await session.get(DeliveryGallery, p.gallery_id) if p else None
            if not g or g.booking_id != b.id:
                raise AccessError('Фото не найдено.', 404)
        # Provider I/O and image decoding must never retain a database lease.
        raw = await self.bytes(p)
        if request.query.get('preview') == '1':
            raw = await asyncio.to_thread(self.preview, raw)
        await self.authorize_staff_download(request, [p])
        return web.Response(body=raw, content_type=image_format(raw)[1])

    async def download_actor(self, session, request):
        actor = request['miniapp_actor']
        person = await session.get(User, actor['id'])
        roles = (await session.scalars(select(UserRole.role).where(UserRole.user_id == actor['id']))).all()
        if not person or not person.active or not roles:
            raise AccessError('Рабочий доступ отключён.', 403)
        return actor | {'roles': roles}

    @staticmethod
    def download_snapshot(photo):
        return photo.id, photo.gallery_id, photo.disk_path, photo.sha256, photo.byte_size

    async def authorize_staff_download(self, request, files):
        async with AsyncSession(self.api.engine) as session:
            actor = await self.download_actor(session, request)
            booking = await self.booking(session, actor, request.match_info['booking'])
            current = (await session.scalars(select(DeliveryPhoto).join(DeliveryGallery,
                DeliveryGallery.id == DeliveryPhoto.gallery_id).where(
                DeliveryGallery.booking_id == booking.id,
                DeliveryPhoto.id.in_([p.id for p in files])))).all()
            if {self.download_snapshot(p) for p in files} != {self.download_snapshot(p) for p in current}:
                raise AccessError('Фотографии изменились. Повторите скачивание.', 409)

    @staticmethod
    def direct_photo_allowed(gallery, photo):
        return bool(photo and photo.gallery_id == gallery.id and (
            photo.upload_set == 'ALL' or photo.origin == 'SALE' if gallery.delivery_mode == 'ALL'
            else photo.selected or photo.upload_set == 'SELECTED'))

    async def authorize_public_download(self, request, files, *, direct=False):
        async with AsyncSession(self.api.engine) as session:
            gallery = await self.public_gallery(request, session)
            query = select(DeliveryPhoto).where(DeliveryPhoto.gallery_id == gallery.id) if direct else self.client_photos_query(gallery)
            query = query.where(guest_photo_clause())
            current = (await session.scalars(query.where(
                DeliveryPhoto.id.in_([p.id for p in files])))).all()
            if direct:
                current = [p for p in current if self.direct_photo_allowed(gallery, p)]
            if {self.download_snapshot(p) for p in files} != {self.download_snapshot(p) for p in current}:
                raise web.HTTPNotFound(text='Фотографии больше не доступны для скачивания.')

    async def bytes(self, photo):
        if photo.origin == 'SALE' and not photo.disk_path.startswith('app:'):
            from types import SimpleNamespace

            from .services.sale_gallery import verify_original
            async with self.download_slots:
                return await verify_original(self.api.engine, self.api.bot, SimpleNamespace(
                    storage_path=photo.disk_path, telegram_file_id=photo.disk_path,
                    byte_size=photo.byte_size, sha256=photo.sha256))
        token, client_id = configured_from_env()
        if not token:
            raise YandexDiskError('Storage unavailable')
        async with self.download_slots:
            return await YandexDisk(token, client_id).download_bytes(photo.disk_path)

    async def public_gallery(self, request, session, *, unlocked=True):
        token = request.match_info['token']
        if not re.fullmatch(r'[A-Za-z0-9_-]{32}', token):
            raise web.HTTPNotFound()
        g = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.access_token == token))
        if not g or not await effective_published(session, g) or (g.expires_at and g.expires_at <= utc_now()):
            raise web.HTTPNotFound(text='Галерея недоступна или срок доступа истёк.')
        if unlocked and g.password_hash:
            cookie = request.cookies.get('pb_gallery_' + str(g.id), '')
            value = hashlib.sha256(g.password_hash.encode()).hexdigest()
            secret = self.api.bot.token.encode()
            expected = hmac.new(secret, (g.access_token + ':' + value).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(cookie, expected):
                raise web.HTTPUnauthorized(text='Введите пароль галереи.')
        return g

    async def unlock(self, request):
        remote = request.remote or 'unknown'
        now = utc_now()
        attempts, until = self.failures.get(remote, (0, now))
        if until <= now:
            attempts = 0
        if attempts >= 10:
            raise web.HTTPTooManyRequests(text='Попробуйте через 15 минут.')
        body = await request.post()
        value = body.get('password', '')
        if not isinstance(value, str) or len(value) > 100:
            raise web.HTTPBadRequest()
        async with AsyncSession(self.api.engine) as session:
            g = await self.public_gallery(request, session, unlocked=False)
            if g.password_hash and not await asyncio.to_thread(password_matches, value, g.password_hash):
                if len(self.failures) > 5000:
                    self.failures = {k:v for k,v in self.failures.items() if v[1] > now}
                self.failures[remote] = (attempts + 1, now + timedelta(minutes=15))
                raise web.HTTPUnauthorized(text='Неверный пароль. Вернитесь в галерею и повторите.')
            response = web.HTTPSeeOther(location='/g/' + g.access_token)
            if g.password_hash:
                stamp = hashlib.sha256(g.password_hash.encode()).hexdigest()
                cookie = hmac.new(self.api.bot.token.encode(), (g.access_token + ':' + stamp).encode(), hashlib.sha256).hexdigest()
                response.set_cookie('pb_gallery_' + str(g.id), cookie, secure=True, httponly=True,
                                    samesite='Lax', path='/g/' + g.access_token, max_age=86400)
            return response

    def document(self, title, body):
        return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title><link rel="stylesheet" href="/app/css/delivery-client.css"><main><h1>' + html.escape(title) + '</h1>' + body + '<footer>Photo Boss</footer></main></html>'

    async def page(self, request):
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            g = await self.public_gallery(request, session, unlocked=False)
            try:
                await self.public_gallery(request, session)
            except web.HTTPUnauthorized:
                body = '<form method="post" action="/g/' + g.access_token + '/unlock"><label>Пароль альбома<input type="password" name="password" maxlength="100" required autocomplete="current-password"></label><button>Открыть фотографии</button></form>'
                return web.Response(text=self.document(g.title, body), content_type='text/html')
            q = self.client_photos_query(g)
            files = (await session.scalars(q.order_by(DeliveryPhoto.id))).all()
            from .gallery_workspace import GalleryWorkspace, bump_counter, read_setting
            if not g.opened_at:
                g.opened_at = utc_now()
            await bump_counter(session, g.id, 'views')
            await session.commit()
            meta = await read_setting(session, f'gallery:meta:{g.booking_id}', {})
            return web.Response(text=GalleryWorkspace(self).document(g, files, meta), content_type='text/html')

    @staticmethod
    def preview(raw):
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((900,900))
            output=io.BytesIO()
            image.save(output,format='JPEG',quality=80)
            return output.getvalue()

    async def client_photo(self, request):
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            g = await self.public_gallery(request, session)
            p = await session.scalar(select(DeliveryPhoto).where(
                DeliveryPhoto.id == int(request.match_info['photo']), guest_photo_clause()))
            if not self.direct_photo_allowed(g, p):
                raise web.HTTPNotFound()
        raw = await self.bytes(p)
        if request.query.get('preview') == '1':
            raw = await asyncio.to_thread(self.preview, raw)
        await self.authorize_public_download(request, [p], direct=True)
        download = request.query.get('download') == '1'
        if download:
            from .gallery_workspace import bump_counter
            async with AsyncSession(self.api.engine) as session:
                g = await self.public_gallery(request, session)
                if not g.downloaded_at:
                    g.downloaded_at = utc_now()
                await bump_counter(session, g.id, 'downloads')
                await session.commit()
        headers = {'Content-Disposition': f'attachment; filename="photo-{p.id}.{image_format(raw)[0]}"'} if download else {}
        return web.Response(body=raw, content_type=image_format(raw)[1], headers=headers)

    async def archive(self, request):
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            g = await self.public_gallery(request, session)
            q = self.client_photos_query(g)
            files = (await session.scalars(q.order_by(DeliveryPhoto.id))).all()
            if not files:
                raise web.HTTPBadRequest(text='Альбом недоступен для скачивания целиком.')
            from .gallery_workspace import archive_selection, bump_counter
            files = archive_selection(request, files)
        response = await self.stream_archive(request, files,
            authorize=lambda: self.authorize_public_download(request, files))
        async with AsyncSession(self.api.engine) as session:
            g = await session.get(DeliveryGallery, g.id)
            if not g:
                return response
            if not g.downloaded_at:
                g.downloaded_at = utc_now()
            await bump_counter(session, g.id, 'downloads')
            await session.commit()
            return response

    async def stream_archive(self, request, files, *, authorize=None):
        async with request.app['delivery_archive_slot']:
            with tempfile.TemporaryFile() as output:
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
                    for p in files:
                        raw = await self.bytes(p)
                        await asyncio.to_thread(archive.writestr, archive_filename(p), raw)
                        del raw
                if authorize is not None:
                    await authorize()
                output.seek(0)
                response = web.StreamResponse(headers={'Content-Type':'application/zip',
                    'Content-Disposition':'attachment; filename="Photo-Boss-Album.zip"',
                    'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
                await response.prepare(request)
                while True:
                    chunk = await asyncio.to_thread(output.read, 65536)
                    if not chunk:
                        break
                    await response.write(chunk)
                await response.write_eof()
        return response

    async def qr(self, request):
        actor = request['miniapp_actor']
        async with AsyncSession(self.api.engine) as session:
            b = await self.booking(session, actor, request.match_info['booking'])
            data = await self.describe(session, actor, b)
            if not data.get('published') or not data.get('clientUrl'):
                raise AccessError('Сначала опубликуйте фотографии для гостя.', 409)
            if data['deliveryMode'] != 'SELECTED':
                raise AccessError('Для QR-доступа выберите выдачу только выбранных фотографий.', 409)
        import qrcode
        output = io.BytesIO()
        qrcode.make(data['clientUrl']).save(output, format='PNG')
        return web.Response(body=output.getvalue(), content_type='image/png')

    async def contacts(self, request):
        from .miniapp_security import require_owner
        require_owner(request['miniapp_actor']['roles'])
        async with AsyncSession(self.api.engine) as session:
            rows = (await session.scalars(select(DeliveryContact).order_by(DeliveryContact.id.desc()).limit(500))).all()
            total = await session.scalar(select(func.count(DeliveryContact.id)))
            subscribers = await session.scalar(select(func.count(DeliveryContact.id)).where(DeliveryContact.marketing_opt_in.is_(True), DeliveryContact.blocked.is_(False)))
            campaigns = (await session.scalars(select(DeliveryCampaign).order_by(DeliveryCampaign.id.desc()).limit(10))).all()
            history = []
            for c in campaigns:
                statuses = (await session.execute(select(DeliveryCampaignRecipient.status, func.count()).where(DeliveryCampaignRecipient.campaign_id == c.id).group_by(DeliveryCampaignRecipient.status))).all()
                history.append({'id':c.id, 'text':c.text, 'statuses':dict(statuses)})
            return web.json_response({'total':total, 'subscribers':subscribers, 'items':[{'id':c.id,'name':c.name,'username':c.username,'phone':c.phone,'subscribed':c.marketing_opt_in,'blocked':c.blocked} for c in rows], 'campaigns':history})

    async def campaign(self, request):
        from .miniapp_security import require_owner
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        body = await self.api.body(request)
        if set(body) != {'text','confirmed'} or body['confirmed'] is not True or not isinstance(body['text'],str) or not 1 <= len(body['text'].strip()) <= 3000:
            raise AccessError('Подтвердите текст сообщения, до 3000 символов.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            campaign = DeliveryCampaign(created_by_id=actor['id'],text=body['text'].strip())
            session.add(campaign)
            await session.flush()
            contacts = (await session.scalars(select(DeliveryContact).where(DeliveryContact.marketing_opt_in.is_(True), DeliveryContact.blocked.is_(False)))).all()
            for c in contacts:
                session.add(DeliveryCampaignRecipient(campaign_id=campaign.id,contact_id=c.id))
            await audit(session, await session.get(User, actor['id']), 'delivery_campaign_created', 'campaign', campaign.id)
            await session.commit()
            return web.json_response({'id':campaign.id,'queued':len(contacts)})

    async def control(self, request):
        actor = request['miniapp_actor']
        start = self.api.day(request.query.get('from', str(self.api.today() - timedelta(days=30))))
        end = self.api.day(request.query.get('to', str(self.api.today())))
        if start > end or (end-start).days > 366:
            raise AccessError('Выберите период до одного года.', 400)
        async with AsyncSession(self.api.engine) as session:
            q = select(Booking).where(Booking.shoot_date.between(start, end))
            q = q.where(booking_scope(actor['id'], actor['roles']))
            bookings = (await session.scalars(q.order_by(Booking.shoot_date, Booking.shoot_time))).all()
            items, by_hotel, by_person = [], {}, {}
            for b in bookings:
                client = await session.get(Client, b.client_id)
                hotel = await session.get(Hotel, b.hotel_id)
                user = await session.get(User, b.photographer_id) if b.photographer_id else None
                shoot = await session.scalar(select(Shooting).where(Shooting.booking_id == b.id))
                sale = await session.scalar(select(Sale).where(Sale.booking_id == b.id).order_by(Sale.id.desc()).limit(1))
                g = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == b.id))
                cancelled = b.status in {'CANCELLED','REJECTED'}
                shot = bool(shoot and (shoot.completed_at or shoot.status in {'SHOT','COMPLETED','READY_FOR_SALE'}))
                paid = bool(sale and sale.payment_status == 'PAID')
                delivered = bool(g and (g.handed_at or g.downloaded_at))
                tasks = [] if cancelled else (['shoot'] if not shot else []) + (['sale'] if shot and not sale else []) + (['payment'] if sale and not paid else []) + (['delivery'] if shot and not delivered else [])
                replies = (await session.scalars(select(DeliveryBookingResponse).where(DeliveryBookingResponse.booking_id == b.id))).all()
                moment = datetime.combine(b.shoot_date,b.shoot_time).replace(tzinfo=self.api.tz).isoformat()
                current_replies = [r for r in replies if r.moment == moment]
                item = {'guestConfirmed':any(r.kind=='CONFIRM' for r in current_replies),'rescheduleRequested':any(r.kind=='RESCHEDULE' for r in current_replies),'id':b.id,'date':str(b.shoot_date),'time':str(b.shoot_time)[:5], 'client':client.name if client else '', 'room':b.room,
                    'hotel':hotel.name if hotel else '', 'photographer':user.name if user else 'Не назначен', 'photographerId':b.photographer_id,
                    'canGallery':can_view(actor,b),'tasks':tasks,'cancelled':cancelled,'shot':shot,'sold':bool(sale),'paid':paid,'delivered':delivered,
                    'paymentStatus':sale.payment_status if sale else None,'openedAt':g.opened_at.isoformat() if g and g.opened_at else None}
                items.append(item)
                for group, key, name in ((by_hotel,b.hotel_id,item['hotel']),(by_person,b.photographer_id,item['photographer'])):
                    row = group.setdefault(key,{'name':name,'booked':0,'shot':0,'sold':0,'cancelled':0})
                    row['booked'] += 1
                    row['shot'] += int(shot)
                    row['sold'] += int(bool(sale))
                    row['cancelled'] += int(cancelled)
            counts = {k:sum(k in i['tasks'] for i in items) for k in ['shoot','sale','payment','delivery']}
            return web.json_response({'items':items,'counts':counts,'hotels':list(by_hotel.values()),'photographers':list(by_person.values()),
                'from':str(start),'to':str(end),'invited':None})


def install_delivery(app, api):
    from .delivery_downloads import install_download_admission
    install_download_admission(app)
    service = Delivery(api)
    app['delivery'] = service
    app['delivery_archive_slot'] = asyncio.Semaphore(1)
    SERVICES[api.bot.token] = service
    @web.middleware
    async def public_headers(request, handler):
        if not request.path.startswith('/g/'):
            return await handler(request)
        try:
            response = await handler(request)
        except YandexDiskError:
            response = web.Response(text='Хранилище временно недоступно. Повторите позже.', status=503)
        except web.HTTPException as exc:
            response = exc
        response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'none'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
        return response
    app.middlewares.append(public_headers)
    app.router.add_get('/api/miniapp/shoot-control', service.control)
    app.router.add_get('/api/miniapp/delivery-contacts', service.contacts)
    app.router.add_post('/api/miniapp/delivery-campaigns', service.campaign)
    app.router.add_get(r'/api/miniapp/delivery/{booking:\d+}/qr', service.qr)
    app.router.add_get(r'/api/miniapp/delivery/{booking:\d+}', service.listing)
    app.router.add_post(r'/api/miniapp/delivery/{booking:\d+}', service.configure)
    app.router.add_get(r'/api/miniapp/delivery/{booking:\d+}/photos/{photo:\d+}', service.staff_photo)
    app.router.add_post(r'/api/miniapp/delivery/{booking:\d+}/photos/{photo:\d+}/selected', service.select_photo)
    app.router.add_get('/g/{token}', service.page)
    app.router.add_post('/g/{token}/unlock', service.unlock)
    app.router.add_get(r'/g/{token}/photos/{photo:\d+}', service.client_photo)
    app.router.add_get('/g/{token}/album.zip', service.archive)
    from .gallery_workspace import install_gallery_workspace
    install_gallery_workspace(app, service)
    return service
