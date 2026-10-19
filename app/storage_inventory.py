"""Check recent originals against the currently connected Disk, not old DB counters."""
import asyncio
import json
import logging
import re
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
    Shooting,
    utc_now,
)
from .yandex_disk import ROOT, YandexDiskError

logger = logging.getLogger(__name__)


async def selected_folder_audit(storage, draft_id, rows, sold):
    """Compare the actual private folder, including files absent from the DB."""
    try:
        items = await storage.list_directory(ROOT + f'/sales/draft-{draft_id}/selected')
    except YandexDiskError as exc:
        if 'HTTP 404' not in str(exc):
            return {'status': 'unavailable', 'diskFiles': None, 'missingSelected': None}
        items = []
    files = [i for i in items if i.get('type') == 'file']
    verified, valid_files = {}, 0
    for item in files:
        match = re.fullmatch(r'([a-f0-9]{64})\.(jpg|png|webp)', item['name'])
        if match and item.get('sha256') == match[1] and type(item.get('size')) is int and item['size'] > 0:
            valid_files += 1
            verified[match[1]] = item
    confirmed = sum(bool(p.sha256 in verified and verified[p.sha256]['size'] == p.byte_size) for p in rows)
    known = {p.sha256 for p in rows}
    return {'status': 'checked', 'diskFiles': len(files), 'verifiedUniqueFiles': len(verified),
            'registeredFiles': len(rows), 'registeredVerified': confirmed,
            'unregisteredFiles': len(set(verified) - known),
            'exactDuplicateFiles': valid_files - len(verified),
            'invalidOrUnexpectedFiles': len(files) - valid_files,
            'missingSelected': max(0, sold - len(verified)) if sold and len(files) == valid_files else None,
            'matchesDeclared': bool(sold and len(verified) == sold and confirmed == len(rows) and len(files) == valid_files)}


async def full_folder_audit(storage, booking_id, shooting_id, declared, selected_drafts=()):
    """Union of this booking's originals; copied exports count once by hash."""
    roots = [(ROOT + f'/delivery/booking-{booking_id}', False)]
    if shooting_id:
        roots.append((ROOT + f'/shootings/{shooting_id}', True))
    roots += [(ROOT + f'/sales/draft-{did}/selected', True) for did in selected_drafts]
    files = []
    async def walk(folder, originals, depth=0):
        if depth > 3:
            raise YandexDiskError('Слишком глубокая структура папки съёмки.')
        try:
            items = await storage.list_directory(folder)
        except YandexDiskError as exc:
            if 'HTTP 404' in str(exc):
                return
            raise
        for item in items:
            name = item['name']
            if item.get('type') == 'dir':
                await walk(folder + '/' + name, originals or name in {'Все фото', 'Выбранные'}, depth + 1)
            elif originals and item.get('type') == 'file' and re.search(r'\.(jpg|jpeg|png|webp)$', name, re.IGNORECASE):
                files.append(item)
    try:
        for folder, originals in roots:
            await walk(folder, originals)
    except (YandexDiskError, ValueError):
        return {'status': 'unavailable', 'uniqueFiles': None, 'missingOriginals': None}
    verified = [f for f in files if re.fullmatch('[a-f0-9]{64}', f.get('sha256', '')) and f.get('size', 0) > 0]
    unique = {f['sha256'] for f in verified}
    return {'status': 'checked', 'diskFiles': len(files), 'uniqueFiles': len(unique) if len(verified) == len(files) else None,
            'exactDuplicateFiles': len(verified) - len(unique), 'unverifiedFiles': len(files) - len(verified),
            'missingOriginals': max(0, declared - len(unique)) if declared and len(verified) == len(files) else None,
            'matchesDeclared': bool(declared and len(unique) == declared and len(verified) == len(files))}


async def sales_storage_audit(engine, storage):
    async with AsyncSession(engine) as session:
        drafts = list(await session.scalars(select(SaleDraft).order_by(SaleDraft.id.desc()).limit(30)))
        selected = list(await session.scalars(select(SaleDraftPhoto).where(SaleDraftPhoto.draft_id.in_([d.id for d in drafts]))))
        shoots = dict((await session.execute(select(Shooting.booking_id, Shooting.id)
            .where(Shooting.booking_id.in_([d.booking_id for d in drafts])))).all())
        all_drafts = (await session.execute(select(SaleDraft.booking_id, SaleDraft.id)
            .where(SaleDraft.booking_id.in_([d.booking_id for d in drafts])))).all()
    result = []
    semaphore = asyncio.Semaphore(3)
    async def inspect(draft):
        async with semaphore:
            folder = await selected_folder_audit(storage, draft.id,
                [p for p in selected if p.draft_id == draft.id], draft.sold_photos)
            full = await full_folder_audit(storage, draft.booking_id, shoots.get(draft.booking_id),
                draft.declared_photo_count, [did for bid, did in all_drafts if bid == draft.booking_id])
            return {'bookingId': draft.booking_id, 'draftId': draft.id, 'draftStatus': draft.status,
                    'declaredTotal': draft.declared_photo_count, 'declaredSold': draft.sold_photos,
                    'selectedFolder': folder, 'fullShootFolders': full}
    result = await asyncio.gather(*(inspect(d) for d in drafts))
    return {'checkedAt': utc_now().isoformat()+'Z', 'items': result, 'limited': len(drafts) == 30}


async def log_sales_storage_audit(engine, storage):
    try:
        async with asyncio.timeout(40):
            data = await sales_storage_audit(engine, storage)
        logger.info('Sale folder reconciliation: %s', json.dumps(data, ensure_ascii=False))
    except (SQLAlchemyError, YandexDiskError, TimeoutError, ValueError, TypeError, KeyError) as exc:
        logger.warning('Sale folder reconciliation unavailable (%s)', type(exc).__name__)


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
                elif digest and data.get('sha256'):
                    item['diskStatus'] = 'verified' if data['sha256'] == digest else 'mismatch'
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
                        'calculatedAmount': (float(json.loads(d.pricing_snapshot)['amount']) if d.pricing_snapshot
                            else float(d.sold_photos*price) if d.sold_photos else None),
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
    async def sale_inventory(request):
        require_owner(request['miniapp_actor']['roles'])
        data = await sales_storage_audit(api.engine, request.app['yandex_disk'])
        return web.json_response(data, headers={'Cache-Control': 'no-store'})
    app.router.add_get('/api/miniapp/storage/sales-audit', sale_inventory)
