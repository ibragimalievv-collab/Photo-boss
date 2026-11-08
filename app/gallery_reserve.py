"""Booking-bound recovery upload, using the existing private delivery pipeline.

No sale, receipt, shooting completion or payment is created here. The workflow
operation ledger owns the final transaction and acknowledgement of each image.
"""
import asyncio
import hashlib
from datetime import timedelta

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .gallery_workspace import read_setting
from .miniapp_security import AccessError
from .models import (
    Booking,
    Client,
    DeliveryGallery,
    DeliveryPhoto,
    Hotel,
    SaleDraft,
    SaleDraftPhoto,
    User,
    utc_now,
)
from .services.core import audit, roles_of
from .services.sale_gallery import storage_identity, storage_location
from .services.sale_workflow import (
    active_draft,
    already_purchased_photo,
    can_sell,
    selected_count,
)
from .yandex_disk import safe_path, validate_saved

KIND = 'gallery_reserve_photo'
SELECTION_KIND = 'sale_gallery_selected'


def upload_data(data):
    if (not isinstance(data, dict) or set(data) != {'booking', 'filename'}
            or type(data['booking']) is not int or not 0 < data['booking'] < 2**31):
        raise AccessError('Выберите назначенную съёмку и исходные фотографии.', 400)
    # The client cannot choose a guest-accessible set, selection or origin.
    return data | {'photoSet': 'ALL'}


class GalleryReserve:
    def __init__(self, delivery):
        self.delivery = delivery
        self.api = delivery.api

    async def booking(self, session, actor, booking_id, *, lock=False):
        person = await session.get(User, actor['id'])
        roles = (await roles_of(session, person)) & set(actor['roles'])
        if not roles & {'OWNER', 'ADMIN', 'PHOTOGRAPHER'}:
            raise AccessError('Нет доступа к загрузке этой съёмки.', 403)
        current_actor = {'id': actor['id'], 'roles': sorted(roles)}
        booking = await self.delivery.booking(session, current_actor, booking_id, edit=lock)
        hotel = await session.get(Hotel, booking.hotel_id)
        photographer = await session.get(User, booking.photographer_id) if booking.photographer_id else None
        client = await session.get(Client, booking.client_id)
        if (booking.status in {'CANCELLED', 'REJECTED'} or not hotel or not hotel.active
                or not photographer or 'PHOTOGRAPHER' not in await roles_of(session, photographer)
                or not client or not client.name or not client.name.strip()
                or not booking.shoot_date or not booking.shoot_time):
            raise AccessError('Нужна действующая запись с именем гостя, отелем и назначенным фотографом.', 409)
        meta = await read_setting(session, f'gallery:meta:{booking.id}', {})
        if meta.get('purged') or meta.get('trashed'):
            raise AccessError('Сначала восстановите галерею из корзины.', 409)
        return current_actor, booking

    async def ensure_folder(self, session, actor, booking):
        gallery = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == booking.id))
        if gallery is None:
            gallery = await self.delivery.ensure(session, actor, booking)
            gallery.delivery_mode = 'SELECTED'
            gallery.published = False
            gallery.auto_publish = True
            defaults = await read_setting(session, 'gallery:defaults', {'days': 180})
            days = defaults.get('days', 180)
            if type(days) is not int or days not in {30, 90, 180, 365}:
                days = 180
            gallery.expires_at = utc_now() + timedelta(days=days)
            await audit(session, await session.get(User, actor['id']), 'gallery_reserve_created', 'booking', booking.id)
        return gallery

    async def create_folder(self, request):
        if await self.api.body(request) != {}:
            raise AccessError('Выберите существующую съёмку. Название берётся из записи гостя.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            actor, booking = await self.booking(session, request['miniapp_actor'], request.match_info['booking'], lock=True)
            await self.ensure_folder(session, actor, booking)
            await session.commit()
            return web.json_response(await self.delivery.describe(session, actor, booking), headers={'Cache-Control': 'no-store'})

    async def prepare_photo(self, actor, data, raw):
        normalized = upload_data(data)
        async with AsyncSession(self.api.engine) as session:
            current_actor, _booking = await self.booking(session, actor, data['booking'])
        identity = storage_identity()
        prepared = await self.delivery.prepare_photo(current_actor, normalized, raw)
        if identity != storage_identity():
            raise AccessError('Подключённый Диск изменился. Повторите загрузку.', 409)
        return {'delivery': prepared, 'storageIdentity': identity}

    async def apply(self, session, actor, kind, data, raw=None, *, prepared=None):
        if kind != KIND:
            raise AccessError('Неизвестная операция загрузки.', 400)
        normalized = upload_data(data)
        if not prepared or not prepared.get('delivery') or prepared.get('storageIdentity') != storage_identity():
            raise AccessError('Сначала подтвердите сохранение оригинала на текущем Диске.', 409)
        current_actor, booking = await self.booking(session, actor, data['booking'], lock=True)
        await self.ensure_folder(session, current_actor, booking)
        result = await self.delivery.apply(session, current_actor, 'delivery_photo', normalized, raw,
                                           prepared=prepared['delivery'])
        photo = await session.get(DeliveryPhoto, result['photoId'])
        # Never turn an existing manually selected/delivered image into a new
        # entitlement. Replays retain the same gallery/photo and original path.
        if not result.get('duplicate'):
            photo.origin = 'RESERVE'
        photo.storage_identity = prepared['storageIdentity']
        await session.flush()
        return result | {'reserved': True, 'guestAccessGranted': False}

    async def sale_access(self, session, actor, booking_id, *, draft=None, lock=False):
        person = await session.get(User, actor['id'])
        roles = (await roles_of(session, person)) & set(actor['roles'])
        booking = await session.get(Booking, booking_id, with_for_update=lock, populate_existing=lock)
        if not person or not await can_sell(person, roles, booking, session=session):
            raise AccessError('Нет доступа к этой продаже.', 403)
        hotel = await session.get(Hotel, booking.hotel_id)
        if not hotel or not hotel.active:
            raise AccessError('Отель недоступен для продажи.', 409)
        meta = await read_setting(session, f'gallery:meta:{booking.id}', {})
        if meta.get('purged') or meta.get('trashed'):
            raise AccessError('Галерея находится в корзине. Сначала восстановите её.', 409)
        current = await active_draft(session, booking.id)
        if current and current.created_by_id != person.id:
            raise AccessError('Эту продажу уже оформляет другой сотрудник.', 403)
        if draft is not None and (draft.booking_id != booking.id or draft.created_by_id != person.id
                                  or draft.status not in {'AWAITING_SELECTED', 'COMPLETED'}):
            raise AccessError('Сначала сохраните чек и количество кадров в этой продаже.', 409)
        return person, roles, booking

    async def source_snapshot(self, session, booking, photo):
        gallery = await session.get(DeliveryGallery, photo.gallery_id) if photo else None
        client = await session.get(Client, booking.client_id)
        context, (_folder, all_folder, _chosen) = await self.delivery.photo_location(session, booking, client)
        if (not photo or not gallery or gallery.booking_id != booking.id
                or photo.origin not in {'RESERVE', 'UPLOAD', 'SHOOTING'} or photo.upload_set != 'ALL'
                or photo.storage_identity != storage_identity()
                or not isinstance(photo.sha256, str) or len(photo.sha256) != 64
                or type(photo.byte_size) is not int or not 0 < photo.byte_size <= 20 * 1024 * 1024):
            raise AccessError('Исходная фотография этой съёмки недоступна. Обновите галерею.', 409)
        try:
            path = safe_path(photo.disk_path)
        except ValueError:
            raise AccessError('Оригинал требует проверки подключённого Диска.', 409) from None
        if photo.origin == 'SHOOTING':
            from .models import Photo, PhotoStorage, Shooting
            from .services.photo_storage import ROOT
            stored = await session.scalar(select(PhotoStorage).join(Photo,
                Photo.id == PhotoStorage.photo_id).join(Shooting,
                Shooting.id == Photo.shooting_id).where(Shooting.booking_id == booking.id,
                    PhotoStorage.shooting_id == Shooting.id, PhotoStorage.disk_path == path,
                    PhotoStorage.sha256 == photo.sha256, PhotoStorage.byte_size == photo.byte_size,
                    PhotoStorage.status == 'STORED', PhotoStorage.stored_at.is_not(None),
                    Shooting.full_upload_completed_at.is_not(None)).execution_options(populate_existing=True))
            if not stored or not path.startswith(ROOT + f'/shootings/{stored.shooting_id}/'):
                raise AccessError('Подтверждённый оригинал полной съёмки изменился. Обновите выбор.', 409)
        elif not path.startswith(all_folder + '/'):
            raise AccessError('Папка исходной съёмки изменилась. Обновите галерею.', 409)
        return {'photo': photo.id, 'gallery': gallery.id, 'path': path, 'sha256': photo.sha256,
                'bytes': photo.byte_size, 'origin': photo.origin,
                'context': context, 'storageIdentity': photo.storage_identity}

    async def import_full_shoot(self, request):
        if await self.api.body(request) != {}:
            raise AccessError('Выберите сохранённую съёмку без дополнительных параметров.', 400)
        from .services.sale_gallery import register_full_shoot
        async with AsyncSession(self.api.engine) as session, session.begin():
            person, _roles, booking = await self.sale_access(session, request['miniapp_actor'],
                int(request.match_info['booking']), lock=True)
            try:
                added = await register_full_shoot(session, person, booking)
            except ValueError as exc:
                raise AccessError(str(exc), 409) from exc
        return web.json_response({'bookingId': int(request.match_info['booking']), 'linkedOriginals': added,
            'guestAccessGranted': False}, headers={'Cache-Control': 'no-store'})

    async def originals(self, request):
        async with AsyncSession(self.api.engine) as session:
            if 'draft' in request.match_info:
                draft = await session.get(SaleDraft, int(request.match_info['draft']))
                if draft is None:
                    raise AccessError('Черновик не найден.', 404)
                booking_id = draft.booking_id
            else:
                draft = None
                booking_id = int(request.match_info['booking'])
            _person, _roles, booking = await self.sale_access(session, request['miniapp_actor'], booking_id, draft=draft)
            draft = draft or await active_draft(session, booking.id)
            accepted = set(await session.scalars(select(SaleDraftPhoto.sha256).where(
                SaleDraftPhoto.draft_id == draft.id))) if draft else set()
            photos = list(await session.scalars(select(DeliveryPhoto).join(DeliveryGallery).where(
                DeliveryGallery.booking_id == booking.id, DeliveryPhoto.upload_set == 'ALL',
                DeliveryPhoto.origin.in_(['RESERVE', 'UPLOAD', 'SHOOTING'])).order_by(DeliveryPhoto.id)))
            result = []
            for photo in photos:
                try:
                    await self.source_snapshot(session, booking, photo)
                except AccessError:
                    continue
                if await already_purchased_photo(session, booking.id, photo.sha256) and photo.sha256 not in accepted:
                    continue
                result.append({'id': photo.id, 'name': photo.filename, 'bytes': photo.byte_size,
                    'accepted': photo.sha256 in accepted,
                    'previewPath': f'/bookings/{booking.id}/gallery-originals/{photo.id}/preview'})
        return web.json_response({'bookingId': booking.id, 'draftId': draft.id if draft else None,
            'photos': result}, headers={'Cache-Control': 'no-store'})

    async def selection_state(self, session, actor, data, *, lock=False):
        from .workflow import target
        if (not isinstance(data, dict) or set(data) != {'draft', 'photo'}
                or type(data['photo']) is not int or not 0 < data['photo'] < 2**31):
            raise AccessError('Выберите исходную фотографию своей съёмки.', 400)
        person = await session.get(User, actor['id'])
        if not person or not await roles_of(session, person):
            raise AccessError('Рабочий доступ отключён.', 403)
        did = await target(session, person, data['draft'], 'draftId')
        preview = await session.get(SaleDraft, did)
        if preview is None:
            raise AccessError('Черновик не найден.', 404)
        # Lock order matches the canonical sale writer: Booking, then Draft.
        person, roles, booking = await self.sale_access(session, actor, preview.booking_id, lock=lock)
        draft = await session.get(SaleDraft, did, with_for_update=lock, populate_existing=lock)
        if draft.created_by_id != person.id or draft.status not in {'AWAITING_SELECTED', 'COMPLETED'}:
            raise AccessError('Сначала сохраните чек и количество кадров.', 409)
        photo = await session.get(DeliveryPhoto, data['photo'], populate_existing=lock)
        snapshot = await self.source_snapshot(session, booking, photo)
        duplicate = await session.scalar(select(SaleDraftPhoto.id).where(
            SaleDraftPhoto.draft_id == did, SaleDraftPhoto.sha256 == photo.sha256))
        if draft.status == 'COMPLETED' and not duplicate:
            raise AccessError('Продажа уже завершена. Новые кадры недоступны.', 409)
        if not duplicate and (await selected_count(session, did) >= (draft.sold_photos or 0)
                              or await already_purchased_photo(session, booking.id, photo.sha256)):
            raise AccessError('Все купленные кадры уже сохранены. Обновите продажу.', 409)
        _folder, context = await storage_location(session, booking, did)
        return person, roles, booking, draft, snapshot, context

    async def prepare_selection(self, actor, data):
        from .disk_sale_media import FILE_PREFIX, reference, verify_saved_reference
        async with AsyncSession(self.api.engine) as session:
            _person, _roles, _booking, draft, snapshot, context = await self.selection_state(session, actor, data)
            paths = list(await session.scalars(select(SaleDraftPhoto.storage_path).where(
                SaleDraftPhoto.draft_id == draft.id, SaleDraftPhoto.storage_path.like(FILE_PREFIX + '%'))))
            references = {path: await reference(session, path) for path in paths}
            did = draft.id
        # Authoritative byte verification is outside every application DB lease.
        async with asyncio.timeout(60):
            raw = await self.read_original(snapshot)
            for saved in references.values():
                await verify_saved_reference(saved)
        if snapshot['storageIdentity'] != storage_identity():
            raise AccessError('Подключённый Диск изменился. Повторите выбор.', 409)
        return {'raw': raw, 'path': snapshot['path'], 'target': did, 'gallery_context': context,
                'references': references, 'sourceSnapshot': snapshot, 'storageIdentity': storage_identity()}

    async def read_original(self, snapshot):
        from .services.media_files import configured_storage
        async with asyncio.timeout(60):
            storage = configured_storage()
            metadata = await storage.metadata(snapshot['path'])
            raw = await storage.download_bytes(snapshot['path'], max_bytes=20 * 1024 * 1024)
            validate_saved(metadata, raw)
            if len(raw) != snapshot['bytes'] or hashlib.sha256(raw).hexdigest() != snapshot['sha256']:
                raise AccessError('Исходная фотография изменилась на Диске. Выберите её заново.', 409)
            await self.delivery.verify_photo(raw)
            return raw

    async def original_preview(self, request):
        booking_id, photo_id = int(request.match_info['booking']), int(request.match_info['photo'])
        async with AsyncSession(self.api.engine) as session:
            _person, _roles, booking = await self.sale_access(session, request['miniapp_actor'], booking_id)
            snapshot = await self.source_snapshot(session, booking, await session.get(DeliveryPhoto, photo_id))
        raw = await self.read_original(snapshot)
        preview = await asyncio.to_thread(self.delivery.preview, raw)
        async with AsyncSession(self.api.engine) as session:
            _person, _roles, booking = await self.sale_access(session, request['miniapp_actor'], booking_id)
            current = await self.source_snapshot(session, booking, await session.get(DeliveryPhoto, photo_id))
            if current != snapshot:
                raise AccessError('Исходная фотография изменилась. Обновите выбор.', 409)
        return web.Response(body=preview, content_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    async def check_selection(self, session, actor, data, prepared):
        if (not prepared or prepared.get('storageIdentity') != storage_identity()
                or not isinstance(prepared.get('raw'), bytes)):
            raise AccessError('Сначала проверьте исходную фотографию на текущем Диске.', 409)
        _person, _roles, _booking, draft, snapshot, context = await self.selection_state(session, actor, data, lock=True)
        if (prepared.get('target') != draft.id or prepared.get('sourceSnapshot') != snapshot
                or prepared.get('gallery_context') != context
                or prepared.get('path') != snapshot['path']
                or len(prepared['raw']) != snapshot['bytes']
                or hashlib.sha256(prepared['raw']).hexdigest() != snapshot['sha256']):
            raise AccessError('Съёмка или оригинал изменились во время чтения. Повторите выбор.', 409)
        return {'draft': {'id': draft.id}}, prepared


def install_gallery_reserve(app, delivery):
    feature = GalleryReserve(delivery)
    app['gallery_reserve'] = feature
    app.router.add_post(r'/api/miniapp/galleries/{booking:\d+}/reserve', feature.create_folder)
    app.router.add_get(r'/api/miniapp/bookings/{booking:\d+}/gallery-originals', feature.originals)
    app.router.add_post(r'/api/miniapp/bookings/{booking:\d+}/gallery-originals/import', feature.import_full_shoot)
    app.router.add_get(r'/api/miniapp/bookings/{booking:\d+}/gallery-originals/{photo:\d+}/preview', feature.original_preview)
    app.router.add_get(r'/api/miniapp/sale-drafts/{draft:\d+}/gallery-originals', feature.originals)
    return feature
