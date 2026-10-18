"""Check recent originals against the currently connected Disk, not old DB counters."""
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    Package,
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


async def recent_sale_audit(engine, *, days=7):
    """Read-only sale diagnostics; do not equate draft balance with sale revenue."""
    local = datetime.now(ZoneInfo(config.training_timezone))
    since = local.date()-timedelta(days=days-1)
    lo = datetime.combine(since, datetime.min.time(), tzinfo=local.tzinfo).astimezone(UTC).replace(tzinfo=None)
    async with AsyncSession(engine) as session:
        drafts = (await session.execute(select(SaleDraft, Booking.shoot_date, Package.price_per_photo,
            select(func.count()).select_from(SaleDraftPhoto).where(SaleDraftPhoto.draft_id == SaleDraft.id).scalar_subquery())
            .join(Booking, Booking.id == SaleDraft.booking_id)
            .join(Package, Package.id == Booking.package_id)
            .where(or_(SaleDraft.created_at >= lo,
                       (Booking.shoot_date >= since) & (Booking.shoot_date <= local.date()),
                       SaleDraft.status.in_(['AWAITING_RECEIPT', 'AWAITING_COUNTS', 'AWAITING_SELECTED'])))
            .order_by(SaleDraft.id.desc()).limit(100))).all()
        sales = list(await session.scalars(select(Sale).where(Sale.created_at >= lo).order_by(Sale.id)))
    return {'checkedAt': utc_now().isoformat()+'Z', 'fromDate': str(since), 'toDate': str(local.date()),
            'drafts': [{'id': d.id, 'bookingId': d.booking_id, 'shootDate': str(day),
                        'status': d.status, 'receiptStored': bool(d.receipt_file_id),
                        'totalFrames': d.declared_photo_count, 'sold': d.sold_photos, 'selected': count,
                        'calculatedAmount': float(d.sold_photos*price) if d.sold_photos else None,
                        'expectedPayment': float(d.expected_amount or 0)} for d, day, price, count in drafts],
            'sales': [{'id': sale.id, 'bookingId': sale.booking_id, 'photos': sale.sold_photos,
                       'amount': float(sale.amount), 'paymentStatus': sale.payment_status} for sale in sales],
            'salesCount': len(sales), 'soldPhotos': sum(sale.sold_photos for sale in sales),
            'salesAmount': float(sum((Decimal(str(sale.amount)) for sale in sales), Decimal(0))), 'draftListLimited': len(drafts) == 100}


async def log_recent_inventory(engine, storage):
    try:
        async with asyncio.timeout(35):
            data = await recent_inventory(engine, storage)
        logger.info('Current Disk recent uploads: %s', json.dumps(data, ensure_ascii=False))
    except asyncio.CancelledError:
        raise
    except (SQLAlchemyError, YandexDiskError, TimeoutError, ValueError, TypeError, KeyError) as exc:
        logger.warning('Current Disk inventory unavailable (%s)', type(exc).__name__)
    try:
        async with asyncio.timeout(15):
            summary = await recent_sale_audit(engine)
        logger.info('Recent sale audit: %s', json.dumps(summary, ensure_ascii=False))
    except asyncio.CancelledError:
        raise
    except (SQLAlchemyError, TimeoutError, ValueError, TypeError, KeyError) as exc:
        logger.warning('Recent sale audit unavailable (%s)', type(exc).__name__)



def install_inventory(app, api):
    async def inventory(request):
        require_owner(request['miniapp_actor']['roles'])
        data = await recent_inventory(api.engine, request.app['yandex_disk'])
        return web.json_response(data, headers={'Cache-Control': 'no-store'})
    app.router.add_get('/api/miniapp/storage/inventory', inventory)
