"""Booking-scoped, read-only selection of existing Yandex Disk sale photos."""
import asyncio
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .disk_folder_audit import child_path, provider_error, read_storage, source_id
from .miniapp_security import AccessError
from .models import Booking, SaleDraftPhoto, Setting, User
from .services.core import audit
from .services.photo_storage import image_format
from .services.sale_workflow import already_purchased_photo, can_sell, selected_count
from .services.shooting_media_audit import inspect_original
from .yandex_disk import YandexDisk, YandexDiskError, safe_disk_read_path

BINDING = 'disk-sale-folder:'
REFERENCE = 'disk-sale-media:'
FILE_PREFIX = 'disk-ref:'
PHOTO = re.compile(r'\.(?:jpe?g|png|webp)$', re.IGNORECASE)
HOSTS = {'disk.yandex.ru', 'disk.yandex.com', 'yadi.sk'}


def parse_source(value):
    if not isinstance(value, str) or len(value) > 1500:
        raise ValueError('Укажите папку или ссылку Яндекс.Диска.')
    value = value.strip()
    if value.startswith('disk:/'):
        return {'kind': 'private', 'root': safe_disk_read_path(value)}
    url = urlsplit(value)
    if (url.scheme != 'https' or url.hostname not in HOSTS or url.username or url.password
            or url.port is not None or url.query or url.fragment):
        raise ValueError('Нужна ссылка Яндекс.Диска или путь disk:/… . Другие сайты не поддерживаются.')
    if url.path == '/client/disk' or url.path.startswith('/client/disk/'):
        return {'kind': 'private', 'root': safe_disk_read_path('disk:/' + unquote(url.path[len('/client/disk'):]).lstrip('/'))}
    if re.fullmatch(r'/(?:d|i)/[A-Za-z0-9_-]{4,200}/?', url.path):
        return {'kind': 'public', 'root': '/', 'publicKey': value.rstrip('/')}
    raise ValueError('Укажите ссылку на опубликованный файл/папку или адрес папки из своего Диска.')


def inside(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def safe_source_path(source, path):
    if source['kind'] == 'private':
        path = safe_disk_read_path(path)
    elif (not isinstance(path, str) or not path.startswith('/') or len(path) > 1000
          or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
          or (path != '/' and any(p in {'', '.', '..'} for p in path[1:].split('/')))):
        raise ValueError('Некорректный путь фотографии.')
    if not inside(path, source['root']):
        raise ValueError('Можно выбрать фотографии только из папки этой съёмки.')
    return path


class Reader:
    def __init__(self, source):
        self.source = source
        self.disk = read_storage() if source['kind'] == 'private' else YandexDisk('')
        if source['kind'] == 'private' and source.get('account') and source['account'] != source_id(self.disk):
            raise ValueError('Подключённый Диск изменился. Владелец должен заново привязать папку съёмки.')

    async def metadata(self, path):
        safe_source_path(self.source, path)
        return (await self.disk.disk_metadata(path) if self.source['kind'] == 'private'
                else await self.disk.public_metadata(self.source['publicKey'], path))

    async def listing(self, path):
        safe_source_path(self.source, path)
        return (await self.disk.disk_list_directory(path) if self.source['kind'] == 'private'
                else await self.disk.public_list_directory(self.source['publicKey'], path))

    async def download(self, path, *, max_bytes=20 * 1024 * 1024):
        safe_source_path(self.source, path)
        return (await self.disk.disk_download_bytes(path, max_bytes=max_bytes) if self.source['kind'] == 'private'
                else await self.disk.public_download_bytes(self.source['publicKey'], path, max_bytes=max_bytes))


async def binding(session, booking_id):
    row = await session.get(Setting, BINDING + str(booking_id))
    return json.loads(row.value) if row else None


async def reference(session, file_id):
    if not isinstance(file_id, str) or not file_id.startswith(FILE_PREFIX):
        return None
    try:
        rid = str(UUID(file_id[len(FILE_PREFIX):]))
    except ValueError as exc:
        raise ValueError('Некорректная привязка фотографии Диска.') from exc
    row = await session.get(Setting, REFERENCE + rid)
    if not row:
        raise ValueError('Привязка фотографии Диска не найдена.')
    return json.loads(row.value)


async def verify_reference(session, file_id):
    saved = await reference(session, file_id)
    if saved is None:
        return None
    data = await Reader(saved['source']).metadata(saved['path'])
    digest = data.get('sha256') or data.get('md5')
    expected = saved['sha256'] if data.get('sha256') else saved['md5']
    if data.get('type') != 'file' or data.get('size') != saved['bytes'] or not digest or digest != expected:
        raise ValueError('Фотография на Диске изменилась. Повторно выберите оригинал перед продажей.')
    return data


async def load_reference(session, file_id, *, max_bytes=20 * 1024 * 1024):
    saved = await reference(session, file_id)
    raw = await Reader(saved['source']).download(saved['path'], max_bytes=max_bytes)
    if len(raw) != saved['bytes'] or hashlib.sha256(raw).hexdigest() != saved['sha256']:
        raise ValueError('Фотография на Диске изменилась; сохранённая версия больше не подтверждена.')
    return raw


async def select_photo(session, actor, booking, draft, data):
    source = await binding(session, booking.id)
    if not source or data['revision'] != source['revision']:
        raise ValueError('Папка съёмки изменилась. Откройте её и выберите фотографии заново.')
    path = safe_source_path(source, data['path'])
    reader = Reader(source)
    metadata = await reader.metadata(path)
    if metadata.get('type') != 'file' or not PHOTO.search(metadata.get('name', '')):
        raise ValueError('Для продажи выберите фотографию JPEG, PNG или WebP.')
    if type(metadata.get('size')) is not int or not 0 < metadata['size'] <= 20 * 1024 * 1024:
        raise ValueError('Фотография должна быть не больше 20 МБ.')
    raw = await reader.download(path)
    image_format(raw)
    result = await asyncio.to_thread(inspect_original, raw, 1, metadata.get('sha256'), metadata['size'])
    digest, md5 = result['sha256'], hashlib.md5(raw, usedforsecurity=False).hexdigest()
    if metadata.get('md5') and metadata['md5'] != md5:
        raise ValueError('Контрольная сумма фотографии изменилась. Повторите выбор.')
    old = await session.scalar(select(SaleDraftPhoto).where(SaleDraftPhoto.draft_id == draft.id, SaleDraftPhoto.sha256 == digest))
    if old:
        return {'photoId': old.id, 'duplicate': True, 'storageVerified': True}
    if draft.status != 'AWAITING_SELECTED':
        raise ValueError('Сначала сохраните количество кадров в продаже.')
    if await selected_count(session, draft.id) >= draft.sold_photos:
        raise ValueError('Все выбранные кадры уже сохранены.')
    if await already_purchased_photo(session, booking.id, digest):
        raise ValueError('Этот кадр уже куплен в предыдущей продаже. Выберите новый кадр для допродажи.')
    rid = str(uuid5(NAMESPACE_URL, f'photo-boss:disk-sale:{draft.id}:{digest}'))
    file_id = FILE_PREFIX + rid
    session.add(Setting(key=REFERENCE + rid, value=json.dumps({
        'source': source, 'path': path, 'bookingId': booking.id, 'sha256': digest, 'md5': md5,
        'bytes': len(raw), 'name': metadata['name']}, ensure_ascii=False)))
    photo = SaleDraftPhoto(draft_id=draft.id, telegram_file_id=file_id,
        telegram_unique_id='disk-selected:' + digest, storage_path=file_id, sha256=digest, byte_size=len(raw))
    session.add(photo)
    await session.flush()
    await audit(session, actor, 'sale_disk_photo_selected', 'sale_draft', draft.id, f'photo={photo.id};bytes={len(raw)}')
    return {'photoId': photo.id, 'storageVerified': True}


class DiskSaleMedia:
    def __init__(self, api):
        self.api = api

    async def authorize(self, session, request, *, lock=False):
        try:
            bid = int(request.match_info['id'])
        except (ValueError, KeyError) as exc:
            raise AccessError('Съёмка не найдена.', 404) from exc
        a = request['miniapp_actor']
        actor = await session.get(User, a['id'])
        booking = await session.get(Booking, bid, with_for_update=lock)
        if not actor or not actor.active or not await can_sell(actor, set(a['roles']), booking, session=session):
            raise AccessError('Нет доступа к продаже этой съёмки.', 403)
        return actor, booking

    async def folder(self, request):
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            actor, booking = await self.authorize(session, request, lock=request.method == 'POST')
            source = await binding(session, booking.id)
            if request.method == 'POST':
                body = await self.api.body(request)
                if not isinstance(body, dict) or set(body) != {'source'}:
                    raise AccessError('Укажите папку или ссылку Яндекс.Диска.', 400)
                try:
                    requested = parse_source(body['source'])
                    if requested['kind'] == 'private' and 'OWNER' not in request['miniapp_actor']['roles']:
                        raise AccessError('Закрытую папку рабочего Диска привязывает владелец. Можно прикрепить доступную ссылку.', 403)
                    reader = Reader(requested)
                    metadata = await reader.metadata(requested['root'])
                    if metadata.get('type') not in {'dir', 'file'}:
                        raise ValueError('Яндекс не подтвердил папку или файл.')
                    if metadata['type'] == 'file' and not PHOTO.search(metadata.get('name', '')):
                        raise ValueError('По ссылке должна быть папка или фотография JPEG, PNG, WebP.')
                    source = requested | {'revision': str(uuid4()), 'type': metadata['type'], 'name': metadata.get('name', 'Яндекс.Диск')}
                    if source['kind'] == 'private':
                        source['account'] = source_id(reader.disk)
                    row = await session.get(Setting, BINDING + str(booking.id))
                    if row:
                        row.value = json.dumps(source, ensure_ascii=False)
                    else:
                        session.add(Setting(key=BINDING + str(booking.id), value=json.dumps(source, ensure_ascii=False)))
                    await audit(session, actor, 'sale_disk_folder_bound', 'booking', booking.id, source['kind'])
                    await session.commit()
                except ValueError as exc:
                    raise AccessError(str(exc), 400) from exc
                except YandexDiskError as exc:
                    raise AccessError(provider_error(exc), 403 if exc.http_status in {401, 403} else 503) from exc
            if not source:
                return web.json_response({'connected': False})
            return web.json_response({'connected': True, 'revision': source['revision'], 'root': source['root'],
                'kind': source['kind'], 'name': source['name'], 'source': source.get('publicKey', source['root'])})

    async def files(self, request):
        async with AsyncSession(self.api.engine) as session:
            _, booking = await self.authorize(session, request)
            source = await binding(session, booking.id)
            if not source:
                raise AccessError('Сначала прикрепите ссылку или выберите папку этой съёмки.', 409)
            try:
                path = safe_source_path(source, request.query.get('path', source['root']))
                reader = Reader(source)
                items = [await reader.metadata(path)] if source['type'] == 'file' else await reader.listing(path)
                rows = []
                for item in items:
                    if source['type'] == 'file':
                        child = path
                    elif source['kind'] == 'private':
                        child = child_path(path, item)
                    else:
                        name = item.get('name')
                        if not isinstance(name, str) or not name or '/' in name:
                            raise ValueError('Некорректное имя файла на Диске.')
                        child = safe_source_path(source, path.rstrip('/') + '/' + name)
                    if item.get('type') == 'dir' or (item.get('type') == 'file' and PHOTO.search(item.get('name', ''))):
                        rows.append({'path': child, 'name': item['name'], 'type': item['type'], 'size': item.get('size')})
                return web.json_response({'path': path, 'root': source['root'], 'revision': source['revision'], 'items': rows})
            except ValueError as exc:
                raise AccessError(str(exc), 400) from exc
            except YandexDiskError as exc:
                raise AccessError(provider_error(exc), 403 if exc.http_status in {401, 403} else 503) from exc


def install_disk_sale_media(app, api):
    feature = DiskSaleMedia(api)
    app.router.add_route('GET', '/api/miniapp/bookings/{id:\\d+}/disk-folder', feature.folder)
    app.router.add_route('POST', '/api/miniapp/bookings/{id:\\d+}/disk-folder', feature.folder)
    app.router.add_get('/api/miniapp/bookings/{id:\\d+}/disk-files', feature.files)
