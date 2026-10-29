"""Atomic sale-photo lineage and current payment-gated guest capabilities.

Linking reuses a confirmed original. It never uploads bytes, changes money,
approves a receipt, or publishes an unfinished draft.
"""
import hashlib
import json
import os
import secrets
from datetime import timedelta

from sqlalchemy import exists, or_, select

from ..models import (
    Booking,
    Client,
    DeliveryGallery,
    DeliveryPhoto,
    Hotel,
    Sale,
    SaleDraft,
    SaleDraftPhoto,
    Setting,
    User,
    utc_now,
)


def storage_identity():
    from ..yandex_disk import configured_from_env
    token, client = configured_from_env()
    read_token = os.getenv('YANDEX_DISK_READ_TOKEN', '').strip() or token
    return hashlib.sha256(json.dumps([token, read_token, client]).encode()).hexdigest()


async def storage_location(session, booking, draft_id):
    from ..gallery_folders import storage_folder
    client = await session.get(Client, booking.client_id)
    hotel = await session.get(Hotel, booking.hotel_id)
    photographer = await session.get(User, booking.photographer_id) if booking.photographer_id else None
    context = (booking.id, booking.hotel_id, hotel.name if hotel else None,
        booking.photographer_id, photographer.name if photographer else None,
        booking.client_id, client.name if client else None,
        str(booking.shoot_date), str(booking.shoot_time), booking.room)
    folder = storage_folder(booking, client.name if client else None, hotel.name if hotel else None,
                            photographer.name if photographer else None)
    return folder + f'/Выбранные/Продажа-{draft_id}', context


def paid_sale_photo_clause(photo=DeliveryPhoto):
    return exists(select(SaleDraftPhoto.id).join(SaleDraft, SaleDraft.id == SaleDraftPhoto.draft_id)
        .join(Sale, Sale.source_draft_id == SaleDraft.id)
        .join(DeliveryGallery, DeliveryGallery.booking_id == SaleDraft.booking_id).where(
            SaleDraftPhoto.delivery_photo_id == photo.id,
            DeliveryGallery.id == photo.gallery_id,
            Sale.booking_id == SaleDraft.booking_id,
            SaleDraft.status == 'COMPLETED', Sale.payment_status == 'PAID',
            photo.storage_identity == storage_identity())).correlate(photo)


def guest_photo_clause(photo=DeliveryPhoto):
    return or_(photo.origin != 'SALE', paid_sale_photo_clause(photo))


async def paid_gallery_ids(session, gallery_ids=None):
    query = select(DeliveryPhoto.gallery_id).where(DeliveryPhoto.origin == 'SALE', paid_sale_photo_clause())
    if gallery_ids is not None:
        query = query.where(DeliveryPhoto.gallery_id.in_(gallery_ids))
    return set(await session.scalars(query.distinct()))


async def blocked_gallery_ids(session, galleries):
    keys = {f'gallery:meta:{g.booking_id}': g.id for g in galleries}
    blocked = set()
    for setting in await session.scalars(select(Setting).where(Setting.key.in_(keys))):
        value = json.loads(setting.value)
        if value.get('purged') or value.get('trashed'):
            blocked.add(keys[setting.key])
    return blocked


async def effective_published(session, gallery, paid_ids=None, blocked_ids=None):
    if blocked_ids is None:
        blocked_ids = await blocked_gallery_ids(session, [gallery])
    if gallery.id in blocked_ids:
        return False
    if not gallery.auto_publish:
        return bool(gallery.published)
    if paid_ids is None:
        paid_ids = await paid_gallery_ids(session, [gallery.id])
    return gallery.id in paid_ids


async def link_photo(session, draft, photo, *, verified=False):
    """Caller holds this booking's transaction lock; FK is the replay identity."""
    booking = await session.get(Booking, draft.booking_id)
    if not booking or photo.draft_id != draft.id:
        raise ValueError('Фотография относится к другой продаже.')
    if draft.status == 'CANCELLED':
        raise ValueError('Продажа отменена. Фотографии не открыты гостю.')
    meta = await session.get(Setting, f'gallery:meta:{booking.id}')
    preferences = json.loads(meta.value) if meta else {}
    if preferences.get('purged') or preferences.get('trashed'):
        raise ValueError('Галерея находится в корзине. Восстановите её перед сохранением продажи.')
    gallery = await session.scalar(select(DeliveryGallery).where(DeliveryGallery.booking_id == booking.id))
    if gallery is None:
        client = await session.get(Client, booking.client_id)
        defaults = await session.get(Setting, 'gallery:defaults')
        days = json.loads(defaults.value).get('days', 180) if defaults else 180
        gallery = DeliveryGallery(booking_id=booking.id, access_token=secrets.token_urlsafe(24),
            title=((client.name if client else '') or 'Фотографии гостя')[:120],
            created_by_id=draft.created_by_id, published=False, auto_publish=True,
            delivery_mode='SELECTED', expires_at=utc_now()+timedelta(days=days) if days else None)
        session.add(gallery)
        await session.flush()
    linked = await session.get(DeliveryPhoto, photo.delivery_photo_id) if photo.delivery_photo_id else None
    if linked and (linked.gallery_id != gallery.id or linked.sha256 != photo.sha256
                   or linked.byte_size != photo.byte_size or linked.upload_set != 'SELECTED'):
        raise ValueError('Связь фотографии с галереей изменилась. Нужна проверка этой продажи.')
    if linked is None:
        linked = await session.scalar(select(DeliveryPhoto).where(DeliveryPhoto.gallery_id == gallery.id,
            DeliveryPhoto.sha256 == photo.sha256, DeliveryPhoto.upload_set == 'SELECTED'))
    if linked and linked.byte_size != photo.byte_size:
        raise ValueError('Размер сохранённого оригинала не совпадает с фотографией продажи.')
    if linked is None:
        name = photo.storage_path.rsplit('/', 1)[-1] if photo.storage_path.startswith('app:') else f'photo-{photo.id}'
        linked = DeliveryPhoto(gallery_id=gallery.id, uploaded_by_id=draft.created_by_id,
            filename=name[:160], disk_path=photo.storage_path, sha256=photo.sha256,
            byte_size=photo.byte_size, selected=True, upload_set='SELECTED', origin='SALE')
        session.add(linked)
        await session.flush()
    if verified:
        # Reattach only an original verified in this request with the same SHA
        # and size; never guess a path on a newly connected Disk.
        linked.disk_path = photo.storage_path
        linked.storage_identity = storage_identity()
    photo.delivery_photo_id = linked.id
    await session.flush()
    return linked


async def link_draft(session, draft, *, verified=False):
    photos = list(await session.scalars(select(SaleDraftPhoto).where(
        SaleDraftPhoto.draft_id == draft.id).order_by(SaleDraftPhoto.id)))
    for photo in photos:
        await link_photo(session, draft, photo, verified=verified)
    return photos


async def gallery_results(session, booking_ids, origin, *, sale_ids=None):
    """Batch summary for workflow/projects; no per-booking publication queries."""
    booking_ids = set(booking_ids)
    if not booking_ids:
        return {}
    galleries = {g.booking_id: g for g in await session.scalars(select(DeliveryGallery).where(
        DeliveryGallery.booking_id.in_(booking_ids)))}
    sales = {}
    query = select(Sale).where(Sale.booking_id.in_(booking_ids)).order_by(Sale.id)
    if sale_ids is not None:
        query = query.where(Sale.id.in_(sale_ids))
    for sale in await session.scalars(query):
        sales[sale.booking_id] = sale
    draft_ids = [s.source_draft_id for s in sales.values() if s.source_draft_id]
    drafts = {d.id: d for d in await session.scalars(select(SaleDraft).where(SaleDraft.id.in_(draft_ids)))}
    photos = {}
    for photo in await session.scalars(select(SaleDraftPhoto).where(SaleDraftPhoto.draft_id.in_(draft_ids))):
        photos.setdefault(photo.draft_id, []).append(photo)
    linked_ids = [p.delivery_photo_id for rows in photos.values() for p in rows if p.delivery_photo_id]
    linked = {p.id: p for p in await session.scalars(select(DeliveryPhoto).where(DeliveryPhoto.id.in_(linked_ids)))}
    paid_ids = await paid_gallery_ids(session, [g.id for g in galleries.values()])
    blocked_ids = await blocked_gallery_ids(session, galleries.values())
    results = {}
    for bid in booking_ids:
        sale, gallery = sales.get(bid), galleries.get(bid)
        accepted = photos.get(sale.source_draft_id, []) if sale else []
        good = [p for p in accepted if p.delivery_photo_id in linked
                and linked[p.delivery_photo_id].gallery_id == (gallery.id if gallery else None)
                and linked[p.delivery_photo_id].sha256 == p.sha256
                and linked[p.delivery_photo_id].byte_size == p.byte_size
                and linked[p.delivery_photo_id].storage_identity == storage_identity()]
        state = 'storage_pending'
        if (sale and gallery and drafts.get(sale.source_draft_id)
                and drafts[sale.source_draft_id].status == 'COMPLETED' and len(good) == sale.sold_photos):
            state = 'payment_pending' if sale.payment_status != 'PAID' else (
                'expired' if gallery.expires_at and gallery.expires_at <= utc_now() else
                'ready' if await effective_published(session, gallery, paid_ids, blocked_ids) else 'closed')
        ready = state == 'ready'
        results[bid] = {'ready': ready, 'saleId': sale.id if sale else None,
            'clientUrl': origin + '/g/' + gallery.access_token if ready else None,
            'qrPath': f'/sales/{sale.id}/gallery-qr' if ready else None,
            'syncPath': f'/sales/{sale.id}/gallery/sync' if sale and sale.source_draft_id else None,
            'photoCount': len(good), 'state': state}
    return results


async def annotate_result(session, actor, roles, result):
    """Replays retain their financial ACK, but never stale payment/access URLs."""
    if not result.get('saleId'):
        return result
    from ..launch_policy import app_url
    from .sale_workflow import can_sell
    sale = await session.get(Sale, result['saleId'])
    booking = await session.get(Booking, sale.booking_id) if sale else None
    closed = {'ready': False, 'saleId': result['saleId'], 'clientUrl': None,
              'qrPath': None, 'syncPath': None, 'photoCount': 0, 'state': 'closed'}
    if not booking or not await can_sell(actor, roles, booking, session=session):
        return result | {'gallery': closed}
    summaries = await gallery_results(session, [booking.id], app_url().split('/app/', 1)[0],
                                     sale_ids=[sale.id])
    return result | {'gallery': summaries[booking.id]}


async def verify_original(engine, bot, photo):
    """Read existing bytes after releasing the pool; never send a client message."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..disk_sale_media import FILE_PREFIX, Reader, reference
    from .media_files import configured_storage, disk_reference
    if photo.storage_path.startswith(FILE_PREFIX):
        async with AsyncSession(engine) as session:
            saved = await reference(session, photo.storage_path)
        if not saved:
            raise ValueError('Привязка оригинала не найдена.')
        raw = await Reader(saved['source']).download(saved['path'])
    elif disk_reference(photo.storage_path):
        raw = await configured_storage().download_bytes(photo.storage_path)
    else:
        import io
        output = io.BytesIO()
        await bot.download(photo.telegram_file_id, destination=output, timeout=40)
        raw = output.getvalue()
    if len(raw) != photo.byte_size or hashlib.sha256(raw).hexdigest() != photo.sha256:
        raise ValueError('Оригинал не совпадает с сохранённой фотографией продажи.')
    return raw
