"""Check recent originals against the currently connected Disk, not old DB counters."""
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import config
from .miniapp_security import require_owner
from .models import (
    Booking,
    DeliveryGallery,
    DeliveryPhoto,
    PhotoStorage,
    Sale,
    SaleDraft,
    SaleDraftPhoto,
    utc_now,
)
from .yandex_disk import YandexDiskError

logger = logging.getLogger(__name__)


async def recent_inventory(engine, storage, *, limit=12):
    async with AsyncSession(engine) as session:
        originals = list(await session.scalars(select(PhotoStorage).order_by(PhotoStorage.created_at.desc()).limit(limit)))
        delivered = (await session.execute(select(DeliveryPhoto, DeliveryGallery.booking_id)
            .join(DeliveryGallery, DeliveryGallery.id == DeliveryPhoto.gallery_id)
            .order_by(DeliveryPhoto.created_at.desc()).limit(limit))).all()
        chosen = (await session.execute(select(SaleDraftPhoto, SaleDraft.booking_id)
            .join(SaleDraft, SaleDraft.id == SaleDraftPhoto.draft_id)
            .order_by(SaleDraftPhoto.created_at.desc()).limit(limit))).all()
        totals = dict((await session.execute(select(PhotoStorage.status, func.count()).group_by(PhotoStorage.status))).all())
        files = [{'source': 'original', 'id': p.photo_id, 'shootingId': p.shooting_id,
                  'at': p.created_at.isoformat()+'Z', 'path': p.disk_path,
                  'bytes': p.byte_size, 'sha256': p.sha256, 'serverStatus': p.status}
                 for p in originals]
        files += [{'source': 'gallery', 'id': p.id, 'bookingId': bid, 'at': p.created_at.isoformat()+'Z',
                   'path': p.disk_path, 'bytes': p.byte_size, 'sha256': p.sha256, 'serverStatus': 'STORED'}
                  for p, bid in delivered]
        files += [{'source': 'sale', 'id': p.id, 'bookingId': bid, 'at': p.created_at.isoformat()+'Z',
                   'path': p.storage_path, 'bytes': p.byte_size, 'sha256': p.sha256, 'serverStatus': 'STORED'}
                  for p, bid in chosen]
    files.sort(key=lambda p: p['at'], reverse=True)
    files = files[:limit]
    semaphore = asyncio.Semaphore(3)

    async def inspect(item):
        path = item.pop('path')
        digest = item.pop('sha256')
        item['diskStatus'] = 'pending'
        if not path:
            return
        async with semaphore:
            try:
                async with asyncio.timeout(12):
                    data = await storage.metadata(path)
                if data.get('type') != 'file' or data.get('size') != item['bytes']:
                    item['diskStatus'] = 'mismatch'
                elif digest and data.get('sha256') == digest:
                    item['diskStatus'] = 'verified'
                else:
                    item['diskStatus'] = 'unverified'
            except YandexDiskError as exc:
                item['diskStatus'] = 'missing' if 'HTTP 404' in str(exc) else 'unavailable'
            except (TimeoutError, ValueError):
                item['diskStatus'] = 'unavailable'
    await asyncio.gather(*(inspect(p) for p in files))
    return {'checkedAt': utc_now().isoformat()+'Z', 'totals': totals, 'items': files,
            'note': 'Проверены последние файлы на подключённом Диске. Список проектов хранится в базе отдельно.'}


async def log_recent_inventory(engine, storage):
    try:
        async with asyncio.timeout(35):
            data = await recent_inventory(engine, storage)
        logger.info('Current Disk recent uploads: %s', json.dumps(data, ensure_ascii=False))
        local = datetime.now(ZoneInfo(config.training_timezone))
        yesterday = local.date()-timedelta(days=1)
        lo = datetime.combine(yesterday, datetime.min.time(), tzinfo=local.tzinfo).astimezone(UTC).replace(tzinfo=None)
        hi = lo+timedelta(days=1)
        async with AsyncSession(engine) as session:
            drafts = (await session.execute(select(SaleDraft, Booking.photographer_id,
                select(func.count()).select_from(SaleDraftPhoto).where(SaleDraftPhoto.draft_id == SaleDraft.id).scalar_subquery())
                .join(Booking, Booking.id == SaleDraft.booking_id)
                .where(or_(Booking.shoot_date == yesterday, (SaleDraft.created_at >= lo) & (SaleDraft.created_at < hi)))
                .order_by(SaleDraft.id.desc()).limit(30))).all()
            sales = list(await session.scalars(select(Sale).where(Sale.created_at >= lo, Sale.created_at < hi).order_by(Sale.id)))
            summary = {'date': str(yesterday), 'drafts': [{'id': d.id, 'bookingId': d.booking_id,
                'status': d.status, 'receiptStored': bool(d.receipt_file_id), 'sold': d.sold_photos,
                'selected': count, 'photographerAssigned': pid is not None} for d, pid, count in drafts],
                'sales': [{'id': s.id, 'bookingId': s.booking_id, 'photos': s.sold_photos,
                           'amount': s.amount, 'paymentStatus': s.payment_status} for s in sales]}
        logger.info('Previous local day sale audit: %s', json.dumps(summary, ensure_ascii=False))
    except asyncio.CancelledError:
        raise
    except (SQLAlchemyError, YandexDiskError, TimeoutError, ValueError, TypeError, KeyError) as exc:
        logger.warning('Current Disk inventory unavailable (%s)', type(exc).__name__)


def install_inventory(app, api):
    async def inventory(request):
        require_owner(request['miniapp_actor']['roles'])
        data = await recent_inventory(api.engine, request.app['yandex_disk'])
        return web.json_response(data, headers={'Cache-Control': 'no-store'})
    app.router.add_get('/api/miniapp/storage/inventory', inventory)
