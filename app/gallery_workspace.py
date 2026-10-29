"""Prototype gallery UI backed exclusively by existing bookings and delivery originals."""
import hashlib
import html
import json
import secrets
import time

from aiohttp import ClientError, web
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .gallery_folders import booking_storage_root, folder_path
from .miniapp_security import AccessError, require_owner
from .models import (
    Booking,
    Client,
    DeliveryClaim,
    DeliveryGallery,
    DeliveryPhoto,
    Hotel,
    Setting,
    User,
)
from .services.booking_access import booking_scope
from .services.core import audit
from .services.sale_gallery import (
    blocked_gallery_ids,
    effective_published,
    paid_gallery_ids,
)


async def read_setting(session, key, default=None):
    row = await session.get(Setting, key)
    return json.loads(row.value) if row else default


async def write_setting(session, key, value):
    await session.execute(text('INSERT INTO settings(key,value) VALUES(:key,:value) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value'), {'key': key, 'value': json.dumps(value)})


async def bump_counter(session, gallery_id, kind):
    await session.execute(text('INSERT INTO settings(key,value) VALUES(:key,\'1\') '
        'ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(settings.value AS INTEGER)+1 AS VARCHAR)'),
        {'key': f'gallery:{kind}:{gallery_id}'})


def archive_selection(request, files):
    raw = request.query.get('ids')
    if raw is None:
        return files
    try:
        ids = {int(value) for value in raw.split(',')}
    except ValueError as exc:
        raise web.HTTPBadRequest(text='Некорректный выбор фотографий.') from exc
    if not ids or len(ids) > 1000 or ids - {p.id for p in files}:
        raise web.HTTPNotFound(text='Фотография недоступна.')
    return [p for p in files if p.id in ids]


class GalleryWorkspace:
    def __init__(self, delivery):
        self.delivery = delivery
        self.api = delivery.api
        self.storage_cache = None

    async def projects(self, request):
        actor = request['miniapp_actor']
        async with AsyncSession(self.api.engine) as session:
            stats = select(DeliveryPhoto.gallery_id, func.count(DeliveryPhoto.id).label('count'),
                func.sum(DeliveryPhoto.byte_size).label('bytes'), func.min(DeliveryPhoto.id).label('first')).group_by(
                DeliveryPhoto.gallery_id).subquery()
            records = (await session.execute(select(Booking, Client.name, Hotel.name, DeliveryGallery,
                stats.c.count, stats.c.bytes, stats.c.first, User.name).outerjoin(Client, Client.id == Booking.client_id)
                .outerjoin(Hotel, Hotel.id == Booking.hotel_id)
                .outerjoin(User, User.id == Booking.photographer_id)
                .outerjoin(DeliveryGallery, DeliveryGallery.booking_id == Booking.id)
                .outerjoin(stats, stats.c.gallery_id == DeliveryGallery.id)
                .where(booking_scope(actor['id'], actor['roles'], photos=True))
                .order_by(Booking.shoot_date.desc(), Booking.shoot_time.desc()))).all()
            keys = ['gallery:defaults']
            for b, _, _, g, *_ in records:
                keys.extend([f'gallery:meta:{b.id}', f'gallery:project-fav:{actor["id"]}:{b.id}'])
                if g:
                    keys.extend([f'gallery:views:{g.id}', f'gallery:downloads:{g.id}'])
            values = {}
            for i in range(0, len(keys), 500):
                values.update({row.key: json.loads(row.value) for row in await session.scalars(
                    select(Setting).where(Setting.key.in_(keys[i:i+500])))})
            requested_covers = [values.get(f'gallery:meta:{b.id}', {}).get('coverId') for b, *_ in records]
            valid_covers = set((await session.execute(select(DeliveryPhoto.id, DeliveryGallery.booking_id)
                .join(DeliveryGallery, DeliveryGallery.id == DeliveryPhoto.gallery_id)
                .where(DeliveryPhoto.id.in_([pid for pid in requested_covers if pid])))).all())
            items = []
            galleries = [record[3] for record in records if record[3]]
            paid_ids = await paid_gallery_ids(session, [g.id for g in galleries])
            blocked_ids = await blocked_gallery_ids(session, galleries)
            for b, client_name, hotel_name, g, count, size, first, photographer_name in records:
                meta = values.get(f'gallery:meta:{b.id}', {})
                if meta.get('purged'):
                    continue
                cover = meta.get('coverId')
                if (cover, b.id) not in valid_covers:
                    cover = first
                items.append({'id': b.id, 'title': g.title if g else f'{hotel_name or "Съёмка"} {str(b.shoot_time)[:5]}',
                    'client': client_name or '', 'hotel': hotel_name or '', 'hotelId': b.hotel_id,
                    'photographer': photographer_name or '', 'photographerId': b.photographer_id,
                    'room': b.room or '', 'guestCount': b.guest_count,
                    'folderPath': folder_path(b, hotel_name, photographer_name),
                    'date': str(b.shoot_date), 'time': str(b.shoot_time)[:5], 'photoCount': count or 0, 'bytes': size or 0,
                    'createdAt': g.created_at.isoformat() if g else str(b.shoot_date),
                    'expiresAt': g.expires_at.isoformat() if g and g.expires_at else None,
                    'published': await effective_published(session, g, paid_ids, blocked_ids) if g else False,
                    'trashed': bool(meta.get('trashed')),
                    'favorite': values.get(f'gallery:project-fav:{actor["id"]}:{b.id}', False),
                    'coverId': cover, 'coverUrl': f'/delivery/{b.id}/photos/{cover}?preview=1' if cover else None,
                    'clientUrl': self.delivery.origin() + '/g/' + g.access_token
                        if g and await effective_published(session, g, paid_ids, blocked_ids) else None,
                    'views': values.get(f'gallery:views:{g.id}', 0) if g else 0,
                    'downloads': values.get(f'gallery:downloads:{g.id}', 0) if g else 0})
            return web.json_response({'items': items, 'storage': {'bytes': sum(x['bytes'] for x in items)},
                'owner': 'OWNER' in actor['roles'], 'canPurge': bool({'OWNER', 'ADMIN'} & set(actor['roles'])),
                'defaults': values.get('gallery:defaults', {'days': 180})})

    async def purge(self, request):
        actor = request['miniapp_actor']
        if not {'OWNER', 'ADMIN'} & set(actor['roles']):
            raise AccessError('Очистка корзины доступна владельцу и администратору.', 403)
        body = await self.api.body(request)
        if body != {'confirm': True} or type(body.get('confirm')) is not bool:
            raise AccessError('Подтвердите безвозвратное удаление фотографий.', 400)
        from .yandex_disk import YandexDisk, YandexDiskError, configured_from_env
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            b = await self.delivery.booking(session, actor, request.match_info['booking'], edit=True)
            meta = await read_setting(session, f'gallery:meta:{b.id}', {})
            if meta.get('purged'):
                return web.json_response({'ok': True, 'bookingId': b.id})
            if not meta.get('trashed'):
                raise AccessError('Сначала переместите проект в корзину.', 409)
            g = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == b.id))
            if g:
                files = (await session.scalars(select(DeliveryPhoto).where(DeliveryPhoto.gallery_id == g.id))).all()
                # Only this booking's delivery folder; no other originals, bookings or sales.
                if files:
                    try:
                        folders = sorted({booking_storage_root(p.disk_path, b.id) for p in files})
                    except ValueError as exc:
                        raise AccessError('Есть фотографии в старом хранилище. Автоматическая очистка недоступна.', 409) from exc
                    token, client_id = configured_from_env()
                    if not token:
                        raise AccessError('Яндекс.Диск недоступен. Проект остаётся в корзине.', 503)
                    disk = YandexDisk(token, client_id)
                    try:
                        for folder in folders:
                            await disk.delete_confirmed(folder)
                    except (YandexDiskError, ClientError, TimeoutError) as exc:
                        raise AccessError('Удаление на Яндекс.Диске ещё не подтверждено. Повторите очистку.', 503) from exc
                await session.execute(delete(DeliveryPhoto).where(DeliveryPhoto.gallery_id == g.id))
                await session.execute(delete(DeliveryClaim).where(DeliveryClaim.gallery_id == g.id))
                g.published = False
                g.access_token = secrets.token_urlsafe(24)
                await session.execute(delete(Setting).where(Setting.key.startswith(f'gallery:photo-fav:{g.id}:')))
            await write_setting(session, f'gallery:meta:{b.id}', {'purged': True})
            await audit(session, await session.get(User, actor['id']), 'gallery_project_purged', 'booking', b.id)
            await session.commit()
            self.storage_cache = None
            return web.json_response({'ok': True, 'bookingId': b.id})

    async def storage(self, request):
        require_owner(request['miniapp_actor']['roles'])
        from .yandex_disk import YandexDisk, YandexDiskError, configured_from_env
        token, client_id = configured_from_env()
        headers = {'Cache-Control': 'no-store'}
        if not token:
            self.storage_cache = None
            return web.json_response({'configured': False, 'available': False}, headers=headers)
        identity = hashlib.sha256(token.encode()).digest()
        if (request.query.get('fresh') != '1' and self.storage_cache
                and self.storage_cache[2] == identity and time.monotonic() - self.storage_cache[0] < 60):
            return web.json_response(self.storage_cache[1], headers=headers)
        self.storage_cache = None
        try:
            data = await YandexDisk(token, client_id)._json('GET', '', params={'fields':'total_space,used_space,user'})
        except YandexDiskError:
            return web.json_response({'configured': True, 'available': False}, headers=headers)
        user = data.get('user') if isinstance(data.get('user'), dict) else {}
        result = {'configured': True, 'available': True, 'used': data.get('used_space'), 'total': data.get('total_space'),
                  'account': {k: user[k] for k in ('login', 'display_name', 'uid') if isinstance(user.get(k), str)},
                  'root': 'Приложения → папка приложения → PhotoBoss'}
        self.storage_cache = (time.monotonic(), result, identity)
        return web.json_response(result, headers=headers)

    async def preferences(self, request):
        actor, body = request['miniapp_actor'], await self.api.body(request)
        if not body or set(body) - {'favorite', 'trashed', 'coverId'}:
            raise AccessError('Некорректная настройка проекта.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            b = await self.delivery.booking(session, actor, request.match_info['booking'], edit=True)
            meta = await read_setting(session, f'gallery:meta:{b.id}', {})
            if 'favorite' in body:
                if type(body['favorite']) is not bool:
                    raise AccessError('Некорректное избранное.', 400)
                await write_setting(session, f'gallery:project-fav:{actor["id"]}:{b.id}', body['favorite'])
            if 'trashed' in body:
                if type(body['trashed']) is not bool:
                    raise AccessError('Некорректная корзина.', 400)
                g = await self.delivery.ensure(session, actor, b)
                if body['trashed'] != bool(meta.get('trashed')):
                    if body['trashed']:
                        meta['wasPublished'] = g.published
                        g.published = False
                    else:
                        g.published = bool(meta.get('wasPublished'))
                        meta.pop('wasPublished', None)
                    meta['trashed'] = body['trashed']
            if 'coverId' in body:
                pid = body['coverId']
                if type(pid) is not int or pid < 1:
                    raise AccessError('Выберите фотографию.', 400)
                p = await session.get(DeliveryPhoto, pid)
                g = await session.get(DeliveryGallery, p.gallery_id) if p else None
                if not g or g.booking_id != b.id:
                    raise AccessError('Фотография недоступна.', 404)
                meta['coverId'] = pid
            await write_setting(session, f'gallery:meta:{b.id}', meta)
            await audit(session, await session.get(User, actor['id']), 'gallery_project_preferences', 'booking', b.id)
            await session.commit()
            return web.json_response({'ok': True})

    async def defaults(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        async with AsyncSession(self.api.engine) as session:
            if request.method == 'POST':
                body = await self.api.body(request)
                if set(body) != {'days'} or type(body['days']) is not int or body['days'] not in {30, 90, 180, 365}:
                    raise AccessError('Выберите срок хранения.', 400)
                await write_setting(session, 'gallery:defaults', body)
                await audit(session, await session.get(User, actor['id']), 'gallery_defaults_changed', 'setting', None)
                await session.commit()
            return web.json_response(await read_setting(session, 'gallery:defaults', {'days': 180}))

    async def favorite(self, request):
        public = request.path.startswith('/g/')
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            if public:
                g = await self.delivery.public_gallery(request, session)
                permitted = (await session.scalars(self.delivery.client_photos_query(g))).all()
            else:
                b = await self.delivery.booking(session, request['miniapp_actor'], request.match_info['booking'])
                g = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == b.id))
                permitted = (await session.scalars(select(DeliveryPhoto).where(
                    DeliveryPhoto.gallery_id == g.id))).all() if g else []
            pid = int(request.match_info['photo'])
            if not g or pid not in {p.id for p in permitted}:
                raise web.HTTPNotFound()
            body = await request.json()
            if set(body) != {'favorite'} or type(body['favorite']) is not bool:
                raise web.HTTPBadRequest()
            # Hearts never change purchased/selected delivery permissions or originals.
            await write_setting(session, f'gallery:photo-fav:{g.id}:{pid}', body['favorite'])
            await session.commit()
            return web.json_response({'photoId': pid, 'favorite': body['favorite']})

    async def enrich(self, session, data, gallery):
        meta = await read_setting(session, f'gallery:meta:{gallery.booking_id}', {}) if gallery else {}
        data['coverId'] = meta.get('coverId')
        data['trashed'] = bool(meta.get('trashed'))
        favorites = {}
        if gallery:
            keys = [f'gallery:photo-fav:{gallery.id}:{p["id"]}' for p in data['photos']]
            for i in range(0, len(keys), 500):
                favorites.update({row.key: json.loads(row.value) for row in await session.scalars(
                    select(Setting).where(Setting.key.in_(keys[i:i+500])))})
        for p in data['photos']:
            p['favorite'] = favorites.get(f'gallery:photo-fav:{gallery.id}:{p["id"]}', False) if gallery else False
        return data

    async def public_data(self, request):
        async with AsyncSession(self.api.engine) as session:
            g = await self.delivery.public_gallery(request, session)
            b = await session.get(Booking, g.booking_id)
            base = '/g/' + g.access_token
            files = (await session.scalars(self.delivery.client_photos_query(g).order_by(DeliveryPhoto.id))).all()
            data = {'title': g.title, 'date': str(b.shoot_date), 'time': str(b.shoot_time)[:5],
                'clientUrl': self.delivery.origin() + base, 'photos': [
                    {'id': p.id, 'name': p.filename, 'url': base + f'/photos/{p.id}', 'uploadSet': p.upload_set}
                    for p in files]}
            await self.enrich(session, data, g)
            # The guest cover must itself belong to the permitted delivery set.
            if data['coverId'] not in {p.id for p in files}:
                data['coverId'] = files[0].id if files else None
            return web.json_response(data)

    async def staff_archive(self, request):
        async with AsyncSession(self.api.engine) as session:
            actor = await self.delivery.download_actor(session, request)
            b = await self.delivery.booking(session, actor, request.match_info['booking'])
            g = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == b.id))
            files = (await session.scalars(select(DeliveryPhoto).where(
                DeliveryPhoto.gallery_id == g.id).order_by(DeliveryPhoto.id))).all() if g else []
            files = archive_selection(request, files)
            if not files:
                raise web.HTTPBadRequest(text='В галерее пока нет фотографий.')
        return await self.delivery.stream_archive(request, files,
            authorize=lambda: self.delivery.authorize_staff_download(request, files))

    def document(self, gallery, files, meta):
        base = '/g/' + gallery.access_token
        cover = next((p for p in files if p.id == meta.get('coverId')), files[0] if files else None)
        title = html.escape(gallery.title)
        cards = ''.join('<figure class="photo"><a href="' + base + f'/photos/{p.id}' + '"><img loading="lazy" src="' + base + f'/photos/{p.id}?preview=1' + '" alt="' + html.escape(p.filename, quote=True) + '"></a><figcaption>' + html.escape(p.filename) + '</figcaption></figure>' for p in files)
        cover_html = '<img src="' + base + f'/photos/{cover.id}?preview=1' + '" alt="Обложка галереи">' if cover else ''
        return '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + title + '</title><link rel="stylesheet" href="/app/css/galleries.css"><script type="module" src="/app/js/gallery-client.js"></script></head><body class="pb-gallery-ui pb-gallery-guest"><div id="guestGallery"><section class="gallery"><header class="gbar"><div class="gbrand">PHOTO BOSS</div></header><div class="hero">' + cover_html + '<div class="herotxt"><small>ПЕРСОНАЛЬНАЯ ГАЛЕРЕЯ</small><h1>' + title + '</h1></div></div><main class="gallerymain"><h2>Фотографии гостей</h2><a class="ghost" href="' + base + '/album.zip">Скачать весь альбом ZIP</a><div class="grid">' + cards + '</div></main></section></div></body></html>'


def install_gallery_workspace(app, delivery):
    service = GalleryWorkspace(delivery)
    app['gallery_workspace'] = service
    app.router.add_get('/api/miniapp/galleries', service.projects)
    app.router.add_get('/api/miniapp/galleries/storage', service.storage)
    app.router.add_get('/api/miniapp/galleries/defaults', service.defaults)
    app.router.add_post('/api/miniapp/galleries/defaults', service.defaults)
    app.router.add_post(r'/api/miniapp/galleries/{booking:\d+}/preferences', service.preferences)
    app.router.add_post(r'/api/miniapp/galleries/{booking:\d+}/purge', service.purge)
    app.router.add_get(r'/api/miniapp/galleries/{booking:\d+}/archive', service.staff_archive)
    app.router.add_post(r'/api/miniapp/galleries/{booking:\d+}/photos/{photo:\d+}/favorite', service.favorite)
    app.router.add_get('/g/{token}/data', service.public_data)
    app.router.add_post(r'/g/{token}/photos/{photo:\d+}/favorite', service.favorite)
    return service
