"""Mini App adapter for existing booking, sale and photo services.

OperationRequest and the caller's user lock provide replay protection. This
module never commits independently of that receipt of the operation.
"""
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .bank_payments import payment_settings
from .miniapp_security import AccessError
from .models import (
    BankPayment,
    Booking,
    Client,
    Hotel,
    HotelAlbumTariff,
    HotelTariff,
    OperationRequest,
    Package,
    Photo,
    PhotoEdit,
    PhotoStorage,
    Receipt,
    Sale,
    SaleDiscountApproval,
    SaleDraft,
    SaleDraftPhoto,
    Shooting,
    User,
    utc_now,
)
from .services.booking_access import booking_scope, can_access_booking
from .services.bookings import create_booking_record
from .services.core import audit
from .services.photo_storage import (
    StorageConfirmationPending,
    ensure_photo_storage,
    image_format,
)
from .services.receipts import (
    expected_amount,
    extract_receipt,
    money,
    operation_key,
    payment_totals,
    warnings_for,
)
from .services.sale_workflow import (
    already_purchased_photo,
    can_sell,
    capture_draft_receipt,
    complete_full_upload,
    complete_sale,
    selected_count,
    set_sale_counts,
    start_sale_draft,
)
from .yandex_disk import (
    ROOT,
    YandexDisk,
    YandexDiskError,
    configured_from_env,
    validate_saved,
)

KINDS = frozenset({'booking', 'sale_start', 'sale_start_inline', 'sale_counts', 'sale_complete', 'sale_receipt',
                   'sale_selected', 'sale_disk_selected', 'sale_gallery_selected', 'booking_receipt', 'shoot_photo', 'shoot_photo_link', 'shoot_complete', 'shoot_retry', 'photo_edit', 'delivery_photo', 'delivery_handoff', 'gallery_reserve_photo'})
MEDIA_KINDS = frozenset({'sale_receipt', 'sale_selected', 'booking_receipt', 'shoot_photo', 'delivery_photo', 'gallery_reserve_photo'})
PREPARED_MEDIA_KINDS = MEDIA_KINDS | {'sale_disk_selected', 'sale_gallery_selected', 'sale_complete'}


def validate_media(kind, raw):
    if kind in MEDIA_KINDS:
        if not raw or len(raw) < 100:
            raise AccessError('Прикрепите изображение.', 400)
        try:
            if kind in {'sale_receipt', 'booking_receipt'}:
                from .services.receipt_documents import manifest
                manifest(raw)
            else:
                image_format(raw)
        except ValueError as exc:
            raise AccessError(str(exc) if kind in {'sale_receipt', 'booking_receipt'} else
                              'Нужен поддерживаемый формат изображения: JPEG или PNG.', 400) from exc
    elif raw is not None:
        raise AccessError('В этой операции не должно быть вложения.', 400)


def media_identity(kind, data, raw):
    return (kind, json.dumps(data, sort_keys=True, ensure_ascii=False),
            hashlib.sha256(raw).hexdigest() if raw is not None else None)


def fields(data, expected):
    if set(data) != set(expected.split()):
        raise AccessError('Некорректные поля рабочей операции.', 400)


SHA256_HEX = frozenset('0123456789abcdef')


def original_name(value):
    """Camera file name for display only; never used as a storage path."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise AccessError('Некорректное имя файла.', 400)
    name = value.replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(ch for ch in name if ch.isprintable()).strip()
    return name[:255] or None


def shoot_fields(kind, data):
    """Full-shoot operations accept an optional camera file name."""
    expected = 'shooting sha256' if kind == 'shoot_photo_link' else 'shooting'
    fields(data, expected + (' filename' if 'filename' in data else ''))
    if kind == 'shoot_photo_link':
        digest = data['sha256']
        if not isinstance(digest, str) or len(digest) != 64 or not set(digest) <= SHA256_HEX:
            raise AccessError('Некорректная контрольная сумма кадра.', 400)
    original_name(data.get('filename'))


def receipt_analysis(value):
    try:
        analysis = json.loads(value or '{}')
    except (ValueError, TypeError):
        analysis = {}
    if not isinstance(analysis, dict):
        analysis = {}
    return analysis


def receipt_state(receipt):
    """Show storage/AI/owner-review separately; never imply a bank confirmation."""
    analysis = receipt_analysis(receipt.analysis)
    extracted = analysis.get('status') == 'extracted' and isinstance(analysis.get('fields'), dict)
    data = analysis['fields'] if extracted else {}
    from .services.automatic_receipts import public_state
    bank = analysis.get('status') == 'bank_confirmed' or bool(analysis.get('bankSettlement'))
    documents = analysis.get('documents') or [{}]
    if not isinstance(documents, list) or any(not isinstance(doc, dict) for doc in documents):
        documents = [{}]
    return {'id': receipt.id, 'status': receipt.status, 'source': analysis.get('provider'),
            'automatic': public_state(receipt), 'bankConfirmed': bank,
            'images': [{'path': f'/receipts/{receipt.id}/image?file={index + 1}', 'mime': doc.get('mime')}
                       for index, doc in enumerate(documents[:2])],
            'stored': bool(receipt.file_id), 'imageAvailable': bool(receipt.file_id),
            'imagePath': f'/receipts/{receipt.id}/image' if receipt.file_id else None,
            'analysisStatus': 'extracted' if extracted else analysis.get('status', 'unavailable'),
            'expectedAmount': str(money(receipt.expected_amount)),
            'extractedAmount': data.get('amount'),
            'warnings': warnings_for(data, receipt.expected_amount, receipt.created_at.date()) if extracted else []}


async def target(session, actor, value, field):
    if not isinstance(value, dict):
        raise AccessError('Не указан объект операции.', 400)
    if set(value) == {'id'} and type(value['id']) is int and value['id'] > 0:
        return value['id']
    if set(value) == {'key'} and isinstance(value['key'], str):
        result = await session.scalar(select(OperationRequest.result).where(
            OperationRequest.user_id == actor.id, OperationRequest.request_key == value['key']))
        if result:
            result = json.loads(result).get(field)
            if type(result) is int and result > 0:
                return result
        raise AccessError('Сначала синхронизируйте предыдущую операцию этой записи.', 424)
    raise AccessError('Некорректная ссылка на объект.', 400)


class Workflow:
    def __init__(self, api):
        self.api = api
        self.tz = getattr(api, 'tz', ZoneInfo('Europe/Moscow'))

    async def photo_hashes(self, request):
        """Checksums already on the server for one shoot, so a re-selected
        folder sends only missing files and reuses guest-selected originals."""
        a = request['miniapp_actor']
        try:
            sid = int(request.match_info['id'])
        except (KeyError, ValueError):
            raise AccessError('Съёмка не найдена.', 404) from None
        from .disk_sale_media import FILE_PREFIX
        async with AsyncSession(self.api.engine) as session:
            shooting = await session.get(Shooting, sid)
            booking = await session.get(Booking, shooting.booking_id) if shooting else None
            if booking is None or not await can_access_booking(session, a['id'], set(a['roles']), booking, photos=True):
                raise AccessError('Нет доступа к съёмке.', 403)
            stored = (await session.scalars(select(PhotoStorage.sha256).where(
                PhotoStorage.shooting_id == sid, PhotoStorage.sha256.is_not(None)))).all()
            selected = (await session.scalars(select(SaleDraftPhoto.sha256).join(
                SaleDraft, SaleDraft.id == SaleDraftPhoto.draft_id).where(
                SaleDraft.booking_id == booking.id,
                ~SaleDraftPhoto.storage_path.like(FILE_PREFIX + '%')))).all()
        return web.json_response({'shootingId': sid, 'stored': sorted(set(stored)),
                                  'selected': sorted(set(selected) - set(stored)),
                                  'open': shooting.status == 'READY_FOR_SALE' and not shooting.full_upload_completed_at},
                                 headers={'Cache-Control': 'no-store'})

    async def receipt_image(self, request):
        return await self._receipt_image(request, draft=False)

    async def draft_receipt_image(self, request):
        return await self._receipt_image(request, draft=True)

    async def receipt_retry(self, request):
        from datetime import UTC, timedelta
        from .people import positive_id
        from .services.automatic_receipts import KEY, analysis_of, enabled
        if not enabled():
            raise AccessError('Автоматическая проверка чеков ещё не включена.', 409)
        actor = request['miniapp_actor']
        rid = positive_id(request.match_info['id'])
        if await self.api.body(request) != {}:
            raise AccessError('Некорректный запрос проверки.', 400)
        async with AsyncSession(self.api.engine) as session, session.begin():
            bid = await session.scalar(select(Receipt.booking_id).where(Receipt.id == rid))
            booking = await session.get(Booking, bid, with_for_update=True) if bid else None
            if not await can_access_booking(session, actor['id'], set(actor['roles']), booking):
                raise AccessError('Чек не найден или недоступен.', 404)
            row = await session.get(Receipt, rid, with_for_update=True)
            analysis = analysis_of(row)
            state = analysis.get(KEY, {})
            if row.status != 'PENDING' or not isinstance(state, dict) or state.get('state') != 'retry_exhausted':
                raise AccessError('Этот чек уже проверен или проверка ещё продолжается.', 409)
            now = datetime.now(UTC)
            try:
                latest = max(datetime.fromisoformat(state.get(key, now.isoformat())) for key in
                             ('updatedAt', 'retryRequestedAt') if key in state)
                if now - latest < timedelta(minutes=1):
                    raise AccessError('Повторная проверка будет доступна через минуту.', 429)
            except (ValueError, TypeError):
                raise AccessError('Обновите состояние чека.', 409) from None
            analysis[KEY] = {'state': 'retry_scheduled', 'attempts': 0, 'codes': [],
                             'retryRequestedAt': now.isoformat(), 'nextAt': now.isoformat()}
            row.analysis = json.dumps(analysis, ensure_ascii=False)
            await audit(session, None, 'receipt_auto_retry_requested', 'receipt', row.id)
        return web.json_response({'ok': True})

    async def _receipt_image(self, request, *, draft):
        from .people import positive_id
        from .services.media_files import LegacyMediaUnavailable, load_private_media
        actor = request['miniapp_actor']
        rid = positive_id(request.match_info['id'])
        async with AsyncSession(self.api.engine) as session:
            row = await session.get(SaleDraft if draft else Receipt, rid) if rid else None
            booking = await session.get(Booking, row.booking_id) if row else None
            if not row or not await can_access_booking(session, actor['id'], set(actor['roles']), booking):
                raise AccessError('Чек не найден или недоступен.', 404)
            if draft and row.created_by_id != actor['id'] and not {'OWNER', 'ADMIN'} & set(actor['roles']):
                raise AccessError('Чек не найден или недоступен.', 404)
            file_id = row.receipt_file_id if draft else row.file_id
            if not file_id:
                raise AccessError('Фото чека ещё не загружено.', 404)
        try:
            raw = await load_private_media(self.api.bot, file_id, max_bytes=16 * 1024 * 1024 + 2048,
                                           standalone=getattr(self.api, 'standalone', None))
        except LegacyMediaUnavailable as exc:
            raise AccessError(str(exc), 409) from exc
        from .services.receipt_ai import document_format
        from .services.receipt_documents import unpack
        documents = unpack(raw)
        index = getattr(request, 'query', {}).get('file', '1')
        if index not in {'1', '2'} or int(index) > len(documents):
            raise AccessError('Вложение чека не найдено.', 404)
        raw = documents[int(index) - 1]
        return web.Response(body=raw, content_type=document_format(raw)[1], headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        })

    async def listing(self, request):
        a = request['miniapp_actor']
        requested = getattr(request, 'query', {}).get('booking')
        if requested is not None and (not requested.isascii() or not requested.isdigit() or not 0 < int(requested) < 2**31):
            raise AccessError('Укажите номер съёмки.', 400)
        async with AsyncSession(self.api.engine) as session:
            query = select(Booking).where(Booking.status.notin_(['CANCELLED', 'REJECTED']))
            query = query.where(booking_scope(a['id'], a['roles']))
            bookings = (await session.scalars(query.order_by(Booking.shoot_date.desc()).limit(200))).all()
            # A large schedule must never hide an unfinished sale from an older
            # shooting. Keep the same server-side role/hotel scope for extras.
            open_sales = await session.scalars(query.where(Booking.id.in_(
                select(SaleDraft.booking_id).where(SaleDraft.status.in_(
                    ['AWAITING_RECEIPT', 'AWAITING_COUNTS', 'AWAITING_SELECTED']))
            )).order_by(Booking.shoot_date.desc()))
            visible_ids = {booking.id for booking in bookings}
            bookings.extend(booking for booking in open_sales if booking.id not in visible_ids)
            if requested is not None:
                chosen = await session.scalar(query.where(Booking.id == int(requested)))
                if chosen is None:
                    raise AccessError('Съёмка не найдена или недоступна.', 404)
                if not any(b.id == chosen.id for b in bookings):
                    bookings.insert(0, chosen)
            packages = (await session.scalars(select(Package))).all()
            prices = {p.id: str(money(p.price_per_photo)) for p in packages}
            assigned_photographers = {u.id: u for u in await session.scalars(select(User).where(
                User.id.in_({b.photographer_id for b in bookings if b.photographer_id})))}
            rows = []
            for b in bookings:
                from .services.pricing import snapshot, unit_price
                effective_price = await unit_price(session, b)
                from .bank_payments import public_bill
                bank_bills = list(await session.scalars(select(BankPayment).where(BankPayment.booking_id == b.id).order_by(BankPayment.id)))
                draft = await session.scalar(select(SaleDraft).where(SaleDraft.booking_id == b.id,
                    SaleDraft.status.in_(['AWAITING_RECEIPT', 'AWAITING_COUNTS', 'AWAITING_SELECTED'])).order_by(SaleDraft.id.desc()).limit(1))
                shoot = await session.scalar(select(Shooting).where(Shooting.booking_id == b.id))
                files = (await session.scalars(select(PhotoStorage).where(PhotoStorage.shooting_id == shoot.id).order_by(PhotoStorage.id))).all() if shoot else []
                names = dict((await session.execute(select(Photo.id, Photo.original_name).where(
                    Photo.shooting_id == shoot.id, Photo.original_name.is_not(None)))).all()) if shoot else {}
                edits = (await session.scalars(select(PhotoEdit).join(PhotoStorage, PhotoStorage.photo_id == PhotoEdit.photo_id).where(PhotoStorage.shooting_id == shoot.id).order_by(PhotoEdit.id))).all() if shoot else []
                purchases = (await session.scalars(select(Sale).where(Sale.booking_id == b.id).order_by(Sale.id))).all()
                receipts = (await session.scalars(select(Receipt).where(Receipt.booking_id == b.id).order_by(Receipt.id))).all()
                can_upload = await can_access_booking(session, a['id'], set(a['roles']), b, photos=True)
                customer = await session.get(Client, b.client_id)
                hotel = await session.get(Hotel, b.hotel_id)
                total, paid, due = await payment_totals(session, b.id)
                original_count = await session.scalar(select(func.count(Photo.id)).where(Photo.shooting_id == shoot.id)) if shoot else 0
                draft_actor = await session.get(User, draft.created_by_id) if draft else None
                from .services.sale_discounts import public_approval
                discounts = list(await session.scalars(select(SaleDiscountApproval).where(
                    SaleDiscountApproval.booking_id == b.id).order_by(SaleDiscountApproval.id.desc())))
                history = {'client': customer.name if customer else '', 'phone': customer.phone if customer else None,
                           'hotel': hotel.name if hotel else 'Отель не указан', 'sales': [
                    {'id': sale.id, 'at': sale.created_at.isoformat() + 'Z', 'photos': sale.sold_photos,
                     'amount': str(money(sale.amount)), 'paymentStatus': sale.payment_status,
                     'lines': (snapshot(sale.pricing_snapshot) or {}).get('lines', [])}
                    for sale in purchases], 'soldTotal': sum(sale.sold_photos for sale in purchases),
                    'salesAmount': str(total), 'paidAmount': str(paid), 'dueAmount': str(due),
                    'creditAmount': str(money(max(Decimal(0), paid - total))),
                    'totalFrames': max([original_count or 0, draft.declared_photo_count or 0 if draft else 0] + [sale.declared_photo_count or 0 for sale in purchases])}
                rows.append({**history, 'receipts': [receipt_state(receipt) for receipt in receipts], 'edits': [{'id': e.id, 'photo': e.photo_id, 'status': e.status, 'error': e.last_error} for e in edits], 'files': [{'id': f.photo_id, 'name': names.get(f.photo_id), 'status': f.status, 'error': (('Хранилище заполнено. Освободите место и повторите.' if 'заполнено' in f.last_error else 'Не удалось сохранить. Повторите загрузку.') if f.last_error else None)} for f in files], 'id': b.id, 'date': str(b.shoot_date), 'time': str(b.shoot_time)[:5],
                    'room': b.room, 'status': b.status, 'hotelId': b.hotel_id, 'price': effective_price,
                    'photographerId': b.photographer_id,
                    'photographer': (assigned_photographers[b.photographer_id].name
                                     if b.photographer_id in assigned_photographers else None),
                    'pricing': snapshot(b.pricing_snapshot),
                    'discountApprovals': [public_approval(discount) for discount in discounts],
                    'declaredDeposit': str(money(b.deposit)),
                    'bankPayments': [public_bill(bill) for bill in bank_bills],
                    'saleRecorded': bool(purchases),
                    'shootingId': shoot.id if shoot else None, 'fullUploaded': bool(shoot and shoot.full_upload_completed_at),
                    'uploadedOriginals': int(original_count or 0),
                    'shootingStatus': shoot.status if shoot else None,
                    'canUpload': can_upload,
                    'canFullUpload': bool(can_upload and shoot and shoot.status == 'READY_FOR_SALE' and not shoot.full_upload_completed_at),
                    'draft': ({'id': draft.id, 'mine': draft.created_by_id == a['id'], 'status': draft.status,
                        'createdByName': draft_actor.name if draft_actor else 'Сотрудник не указан',
                        'pricing': snapshot(draft.pricing_snapshot),
                        'amount': ((snapshot(draft.pricing_snapshot) or {}).get('amount') or
                            (str(money(Decimal(effective_price or '0') * draft.sold_photos)) if draft.sold_photos else None)),
                        'expectedAmount': (str(money(max(Decimal(0), total + Decimal(
                            (snapshot(draft.pricing_snapshot) or {}).get('amount') or
                            str(money(Decimal(effective_price or '0') * draft.sold_photos))) - paid)))
                            if draft.sold_photos else None),
                        'receiptStored': bool(draft.receipt_file_id),
                        'receiptImagePath': (f'/sale-drafts/{draft.id}/receipt/image'
                            if draft.receipt_file_id and (draft.created_by_id == a['id'] or {'OWNER', 'ADMIN'} & set(a['roles'])) else None),
                        'receiptAnalysisStatus': receipt_analysis(draft.receipt_analysis).get('status', 'unavailable'),
                        'receiptImages': [{'path': f'/sale-drafts/{draft.id}/receipt/image?file={i + 1}'}
                                          for i in range(min(2, len(receipt_analysis(draft.receipt_analysis).get('documents') or [{}])))],
                        'total': draft.declared_photo_count, 'sold': draft.sold_photos,
                        'selected': await selected_count(session, draft.id)} if draft else None)})
            hotel_query = select(Hotel).where(Hotel.active.is_(True))
            if 'ADMIN' in a['roles'] and 'OWNER' not in a['roles']:
                from .models import HotelEmployee
                hotel_query = hotel_query.where(Hotel.id.in_(select(HotelEmployee.hotel_id).where(
                    HotelEmployee.user_id == a['id'])))
            hotels = (await session.scalars(hotel_query)).all()
            tariffs = list(await session.scalars(select(HotelTariff).where(
                HotelTariff.hotel_id.in_([h.id for h in hotels]), HotelTariff.active.is_(True)).order_by(HotelTariff.id)))
            albums = list(await session.scalars(select(HotelAlbumTariff).where(
                HotelAlbumTariff.hotel_id.in_([h.id for h in hotels]), HotelAlbumTariff.active.is_(True))
                .order_by(HotelAlbumTariff.id)))
            hotel_rows = []
            for hotel in hotels:
                units = [t for t in tariffs if t.hotel_id == hotel.id and t.kind == 'UNIT']
                fixed = next((t for t in units if t.id == hotel.fixed_unit_tariff_id), None)
                if fixed is None and len(units) == 1:
                    fixed = units[0]
                display_price = str(money(fixed.price)) if fixed else None
                # Mirror server booking readiness for the historical seeded
                # trio. Only a unique existing active package price is shown;
                # distinct package choices must remain an explicit selection.
                active_packages = [package for package in packages if package.active]
                if display_price is None and len({money(package.price_per_photo) for package in active_packages}) == 1:
                    from .services.pricing import booking_unit_quote
                    try:
                        display_price = (await booking_unit_quote(session, hotel.id, active_packages[0].id))['price']
                    except ValueError:
                        pass
                hotel_rows.append({'id': hotel.id, 'name': hotel.name,
                    'fixedUnitTariffId': fixed.id if fixed else None, 'unitPrice': display_price})
            from .launch_policy import app_url
            from .services.sale_gallery import gallery_results
            galleries = await gallery_results(session, [row['id'] for row in rows],
                                               app_url().split('/app/', 1)[0])
            for row in rows:
                row['gallery'] = galleries[row['id']]
            from .services.inline_sale import options
            inline_sale = await options(session, a['id'], set(a['roles']))
            inline_sale['time'] = datetime.now(self.tz).strftime('%H:%M')
        from .services.automatic_receipts import enabled as receipt_auto_enabled
        return web.json_response({'bookings': rows, 'hotels': hotel_rows,
            'receiptAutomatic': receipt_auto_enabled(),
            'inlineSale': inline_sale,
            'tariffs': [{'id': t.id, 'hotelId': t.hotel_id, 'name': t.name, 'kind': t.kind,
                        'price': str(money(t.price)), 'photoCount': t.photo_count} for t in tariffs],
            'canConfigurePricing': bool({'OWNER', 'ADMIN'} & set(a['roles'])),
            'albums': [{'id': album.id, 'hotelId': album.hotel_id, 'name': album.name,
                        'price': str(money(album.price))} for album in albums],
            'canApproveDiscount': bool({'OWNER', 'ADMIN'} & set(a['roles'])),
            'canAuditDisk': 'OWNER' in a['roles'],
            'canCheckBankConnection': 'OWNER' in a['roles'],
            **payment_settings(refund_integration=getattr(self.api, 'booking_refunds', None)),
            'packages': [{'id': p.id, 'name': p.name, 'price': prices[p.id]} for p in packages if p.active],
            'canBook': bool({'OWNER', 'ADMIN', 'MANAGER'} & set(a['roles'])),
            'date': str(self.api.today()), 'actorId': a['id']})

    async def save_telegram(self, actor, raw, *, receipt=False):
        # Keep the historic adapter name for bot compatibility. Standalone
        # uploads always use private Disk, independently of notification policy.
        from .services.media_files import direct_disk_mode
        if direct_disk_mode(getattr(self.api, 'standalone', None)):
            from .services.media_files import save_private_media
            return await save_private_media(actor.id, raw, receipt=receipt)
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
        from aiogram.types import BufferedInputFile
        ext, _ = image_format(raw)
        upload = BufferedInputFile(raw, filename=f'photo-boss.{ext}')
        try:
            if receipt:
                try:
                    message = await self.api.bot.send_photo(
                        actor.tg_id, upload, caption='Photo Boss · чек на проверку', protect_content=True,
                    )
                    attachment = message.photo[-1]
                except TelegramBadRequest as exc:
                    # Some valid originals cannot be represented as Telegram photos.
                    if 'PHOTO_INVALID_DIMENSIONS' not in str(exc):
                        raise
                    message = await self.api.bot.send_document(
                        actor.tg_id, BufferedInputFile(raw, filename=f'photo-boss.{ext}'),
                        caption='Photo Boss · чек на проверку (файл)', protect_content=True,
                    )
                    attachment = message.document
            else:
                message = await self.api.bot.send_document(actor.tg_id, upload, caption='Photo Boss · сохранённый кадр', protect_content=True)
                attachment = message.document
            return attachment.file_id, attachment.file_unique_id
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            inaccessible = isinstance(exc, TelegramForbiddenError) or any(
                value in str(exc).casefold() for value in ('chat not found', 'user is deactivated')
            )
            if not inaccessible:
                raise
            # The web app must work even before a staff member starts/unblocks
            # the bot. Preserve the original privately, with no Telegram message
            # or public link required and no success before checksum validation.
            from .services.media_files import save_private_media
            return await save_private_media(actor.id, raw, receipt=receipt)

    async def save_selected(self, draft, raw):
        from .services.sale_gallery import storage_location
        async with AsyncSession(self.api.engine) as session:
            booking = await session.get(Booking, draft.booking_id)
            folder, _context = await storage_location(session, booking, draft.id)
        return await self.save_disk_original(folder[len(ROOT):], raw)

    async def save_disk_original(self, folder, raw):
        token, client_id = configured_from_env()
        if not token:
            raise AccessError('Яндекс.Диск не подключён. Фото остаётся в локальной очереди.', 503)
        ext, mime = image_format(raw)
        digest = hashlib.sha256(raw).hexdigest()
        storage = YandexDisk(token, client_id)
        path = ROOT + folder + f'/{digest}.{ext}'
        try:
            validate_saved(await storage.metadata(path), raw)
            return path
        except YandexDiskError as exc:
            if not any(reason in str(exc) for reason in ('HTTP 404', 'не совпадает')):
                raise
        try:
            await storage.upload_bytes(path, raw, content_type=mime)
        except YandexDiskError as exc:
            if not any(code in str(exc) for code in ('HTTP 404', 'HTTP 409')):
                raise
            parent = ROOT
            for part in folder.strip('/').split('/'):
                parent += '/' + part
                await storage.ensure_dir(parent)
            await storage.upload_bytes(path, raw, content_type=mime)
        return path

    async def prepare_media(self, actor_data, kind, data, raw=None):
        """Authorize briefly, then release the DB connection before provider I/O.

        This is preparation only: no sale, receipt, photo or operation-ledger row
        is written here. apply() repeats authorization and business-state checks
        under the original transactional user/booking locks before linking a
        confirmed original. A concurrent change may leave a private orphan, but
        never a falsely acknowledged upload or a second financial operation.
        """
        if kind not in PREPARED_MEDIA_KINDS:
            return None
        validate_media(kind, raw)
        prepared = {'identity': media_identity(kind, data, raw), 'references': {}}
        try:
            if kind == 'sale_gallery_selected':
                from .delivery import Delivery
                from .gallery_reserve import GalleryReserve
                prepared.update(await GalleryReserve(Delivery(self.api)).prepare_selection(actor_data, data))
                return prepared
            if kind == 'delivery_photo':
                from .delivery import Delivery
                prepared['delivery'] = await Delivery(self.api).prepare_photo(actor_data, data, raw)
                return prepared
            if kind == 'gallery_reserve_photo':
                from .delivery import Delivery
                from .gallery_reserve import GalleryReserve
                prepared['reserve'] = await GalleryReserve(Delivery(self.api)).prepare_photo(actor_data, data, raw)
                return prepared
            async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
                actor = await session.get(User, actor_data['id'])
                roles = set(actor_data['roles'])
                if actor is None or not actor.active:
                    raise AccessError('Доступ отключён.', 403)
                if kind.startswith('sale_'):
                    expected = {'sale_receipt': 'draft', 'sale_selected': 'draft',
                                'sale_disk_selected': 'draft revision path', 'sale_complete': 'draft price'}[kind]
                    fields(data, expected)
                    did = await target(session, actor, data['draft'], 'draftId')
                    draft = await session.get(SaleDraft, did)
                    if draft is None:
                        raise AccessError('Черновик не найден.', 404)
                    booking = await session.get(Booking, draft.booking_id)
                    if draft.created_by_id != actor.id or not await can_sell(actor, roles, booking, session=session):
                        raise AccessError('Нет доступа к этой продаже.', 403)
                    prepared['target'] = did
                    if kind == 'sale_receipt' and draft.status != 'AWAITING_RECEIPT':
                        raise AccessError('Этот черновик продажи уже закрыт.', 409)
                    if kind == 'sale_selected':
                        from .services.sale_gallery import storage_location
                        _folder, prepared['gallery_context'] = await storage_location(session, booking, did)
                        digest = hashlib.sha256(raw).hexdigest()
                        old = await session.scalar(select(SaleDraftPhoto).where(
                            SaleDraftPhoto.draft_id == did, SaleDraftPhoto.sha256 == digest))
                        if draft.status == 'COMPLETED' and old:
                            prepared['completed_duplicate'] = True
                        elif draft.status != 'AWAITING_SELECTED':
                            raise AccessError('Сначала сохраните чек и количество кадров.', 409)
                        elif not old and await selected_count(session, did) >= draft.sold_photos:
                            raise AccessError('Все выбранные кадры уже сохранены.', 409)
                        elif not old and await already_purchased_photo(session, booking.id, digest):
                            raise AccessError('Этот кадр уже куплен в предыдущей продаже. Выберите новый кадр для допродажи.', 409)
                    from .disk_sale_media import binding, reference
                    if kind == 'sale_disk_selected':
                        source = await binding(session, booking.id)
                        if not source or source.get('revision') != data['revision']:
                            raise AccessError('Папка съёмки изменилась. Откройте её и выберите фотографии заново.', 409)
                        prepared['source'] = source
                    # An already completed sale only needs its transactional
                    # duplicate acknowledgement; provider availability must
                    # not prevent the original queued completion from replaying.
                    if kind != 'sale_receipt' and draft.status != 'COMPLETED':
                        external = list(await session.scalars(select(SaleDraftPhoto.storage_path).where(
                            SaleDraftPhoto.draft_id == did, SaleDraftPhoto.storage_path.like('disk-ref:%'))))
                        prepared['references'] = {file_id: await reference(session, file_id) for file_id in external}
                elif kind == 'booking_receipt':
                    fields(data, 'booking purpose')
                    bid = await target(session, actor, data['booking'], 'bookingId')
                    booking = await session.get(Booking, bid)
                    if not await can_access_booking(session, actor.id, roles, booking):
                        raise AccessError('Нет доступа к записи.', 403)
                    if data['purpose'] not in ('DEPOSIT', 'PAYMENT'):
                        raise AccessError('Неизвестный вид чека.', 400)
                    from .services.automatic_receipts import enabled, existing_upload, pending_upload
                    if enabled() and await existing_upload(session, bid, data['purpose'], hashlib.sha256(raw).hexdigest()):
                        prepared['target'] = bid
                        return prepared
                    pending = await pending_upload(session, bid, data['purpose'])
                    if pending:
                        raise AccessError(f'Чек №{pending} уже ожидает проверки.', 409)
                    if await expected_amount(session, booking, data['purpose']) <= 0:
                        raise AccessError('Дополнительная оплата не требуется.', 409)
                    prepared['target'] = bid
                else:
                    shoot_fields(kind, data)
                    sid = await target(session, actor, data['shooting'], 'shootingId')
                    shooting = await session.get(Shooting, sid)
                    booking = await session.get(Booking, shooting.booking_id) if shooting else None
                    if not await can_access_booking(session, actor.id, roles, booking, photos=True):
                        raise AccessError('Нет доступа к съёмке.', 403)
                    old = await session.scalar(select(PhotoStorage.id).where(PhotoStorage.shooting_id == sid,
                        PhotoStorage.sha256 == hashlib.sha256(raw).hexdigest()).limit(1))
                    if not old and (shooting.status != 'READY_FOR_SALE' or shooting.full_upload_completed_at):
                        raise AccessError('Загрузка новых кадров уже закрыта или ещё недоступна.', 409)
                    prepared['target'] = sid
            # Detached identity/targets above are all that provider calls need.
            # No connection, transaction or business row lock remains checked out.
            if kind in {'sale_receipt', 'booking_receipt'}:
                prepared['file'] = await self.save_telegram(actor, raw, receipt=True)
                prepared['analysis'] = await extract_receipt(raw)
                from .services.receipt_documents import manifest
                prepared['analysis']['documents'] = manifest(raw)
            elif kind == 'sale_selected' and not prepared.get('completed_duplicate'):
                prepared['path'] = await self.save_selected(draft, raw)
            elif kind == 'shoot_photo':
                from .services.media_files import direct_disk_mode
                if direct_disk_mode(getattr(self.api, 'standalone', None)):
                    prepared['path'] = await self.save_disk_original(f'/shootings/{sid}', raw)
                else:
                    prepared['file'] = await self.save_telegram(actor, raw)
            if kind == 'sale_disk_selected':
                from .disk_sale_media import prepare_selection, selection_reference
                prepared['selection'] = await prepare_selection(prepared['source'], data)
                file_id, saved = selection_reference(prepared['target'], booking.id, prepared['selection'])
                prepared['references'][file_id] = saved
            if prepared['references']:
                from .disk_sale_media import verify_saved_reference
                # The incoming Disk original has just been downloaded and
                # checksum-verified by prepare_selection; verify earlier refs.
                incoming = file_id if kind == 'sale_disk_selected' else None
                for known_id, saved in prepared['references'].items():
                    if known_id == incoming:
                        continue
                    await verify_saved_reference(saved)
            return prepared
        except AccessError:
            raise
        except ValueError as exc:
            raise AccessError(str(exc), 409) from exc
        except YandexDiskError as exc:
            if kind == 'sale_disk_selected':
                from .disk_folder_audit import provider_error
                raise AccessError(provider_error(exc), exc.http_status if exc.http_status in {401, 403, 404} else 503) from exc
            raise AccessError('Яндекс.Диск временно недоступен. Вложение сохранено локально.', 503) from exc

    async def complete_prepared_sale(self, session, actor, draft_id, prepared_media):
        if prepared_media is None:
            return await complete_sale(session, actor, draft_id)
        return await complete_sale(session, actor, draft_id,
                                   verified_references=prepared_media['references'])

    async def apply(self, conn, actor_data, kind, data, raw=None, *, prepared_media=None):
        validate_media(kind, raw)
        if prepared_media is not None and prepared_media['identity'] != media_identity(kind, data, raw):
            raise AccessError('Подготовленное вложение не соответствует операции.', 409)
        try:
            async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                actor = await session.get(User, actor_data['id'])
                roles = set(actor_data['roles'])
                if kind == 'booking':
                    data = {'deposit': '1000', **data}
                    fields(data, 'client_name client_phone hotel_id package_id room guest_count deposit shoot_date shoot_time photographer_id')
                    if data['photographer_id'] is not None and type(data['photographer_id']) is not int:
                        raise AccessError('Некорректный фотограф.', 400)
                    booking = await create_booking_record(session, actor, data, data['photographer_id'])
                    result = {'bookingId': booking.id}
                elif kind.startswith('delivery_'):
                    from .delivery import Delivery
                    result = await Delivery(self.api).apply(session, actor_data, kind, data, raw,
                        **({'prepared': prepared_media['delivery']} if prepared_media is not None else {}))
                elif kind == 'gallery_reserve_photo':
                    from .delivery import Delivery
                    from .gallery_reserve import GalleryReserve
                    result = await GalleryReserve(Delivery(self.api)).apply(session, actor_data, kind, data, raw,
                        prepared=prepared_media['reserve'] if prepared_media is not None else None)
                elif kind == 'photo_edit':
                    from .photo_edits import apply
                    result = await apply(session, actor_data, data)
                elif kind == 'sale_gallery_selected':
                    from .delivery import Delivery
                    from .gallery_reserve import GalleryReserve
                    normalized, selected = await GalleryReserve(Delivery(self.api)).check_selection(
                        session, actor_data, data, prepared_media)
                    result = await self.sale(session, actor, roles, 'sale_selected', normalized,
                                             selected['raw'], prepared_media=selected)
                elif kind.startswith('sale_'):
                    result = await self.sale(session, actor, roles, kind, data, raw, prepared_media=prepared_media)
                elif kind == 'booking_receipt':
                    result = await self.receipt(session, actor, roles, data, raw, prepared_media=prepared_media)
                else:
                    result = await self.shoot(session, actor, roles, kind, data, raw, prepared_media=prepared_media)
                await session.flush()
                # Bound to the outer transaction; no independent financial commit.
                if kind.startswith('sale_'):
                    from .services.sale_gallery import annotate_result
                    result = await annotate_result(session, actor, roles, result)
                return result
        except AccessError:
            raise
        except StorageConfirmationPending as exc:
            raise AccessError(str(exc), 424) from exc
        except ValueError as exc:
            raise AccessError(str(exc), 409) from exc
        except YandexDiskError as exc:
            if kind == 'sale_disk_selected':
                from .disk_folder_audit import provider_error
                raise AccessError(provider_error(exc), exc.http_status if exc.http_status in {401, 403, 404} else 503) from exc
            raise AccessError('Яндекс.Диск временно недоступен. Вложение сохранено локально.', 503) from exc

    async def sale(self, session, actor, roles, kind, data, raw, *, prepared_media=None):
        if kind == 'sale_start_inline':
            from .services.inline_sale import create
            fields(data, 'guest price tariffId' if 'tariffId' in data else 'guest price')
            draft = await create(session, actor, roles, data['guest'], now=datetime.combine(
                self.api.today(), datetime.now(self.tz).time().replace(tzinfo=None)))
        elif kind == 'sale_start':
            fields(data, 'booking price tariffId' if 'tariffId' in data else 'booking price')
            bid = await target(session, actor, data['booking'], 'bookingId')
            draft = await start_sale_draft(session, actor, roles, bid)
        else:
            expected = {'sale_counts': 'draft total sold price', 'sale_complete': 'draft price',
                        'sale_receipt': 'draft', 'sale_selected': 'draft', 'sale_disk_selected': 'draft revision path'}[kind]
            optional = ('tariffId', 'discountId', 'albumId', 'albumQuantity', 'photoAmount') if kind == 'sale_counts' else ()
            fields(data, expected + ''.join(' ' + key for key in optional if key in data))
            did = await target(session, actor, data['draft'], 'draftId')
            if prepared_media is not None and prepared_media['target'] != did:
                raise AccessError('Черновик изменился. Повторите отправку.', 409)
            preview = await session.get(SaleDraft, did)
            if preview is None:
                raise AccessError('Черновик не найден.', 404)
            await session.get(Booking, preview.booking_id, with_for_update=True)
            draft = await session.get(SaleDraft, did, with_for_update=True, populate_existing=True)
        booking = await session.get(Booking, draft.booking_id)
        if draft.created_by_id != actor.id or not await can_sell(actor, roles, booking, session=session):
            raise AccessError('Нет доступа к этой продаже.', 403)
        if 'price' in data:
            from .services.pricing import unit_price
            # A hotel's new rate cannot replace the immutable booking rate.
            # Package purchases remain explicit; counted drafts retain their snapshot.
            explicit_tariff = kind in {'sale_start', 'sale_start_inline', 'sale_counts'} and data.get('tariffId') is not None
            if kind in {'sale_start', 'sale_start_inline'} and explicit_tariff:
                from .models import HotelTariff
                from .services.pricing import snapshot
                fixed = snapshot(booking.pricing_snapshot) or {}
                tariff = await session.get(HotelTariff, data['tariffId'])
                is_booking_rate = fixed.get('tariffId') == data['tariffId'] and fixed.get('kind') == 'UNIT'
                if (not tariff or tariff.hotel_id != booking.hotel_id
                        or (not tariff.active and not is_booking_rate)):
                    raise AccessError('Тариф недоступен в этом отеле.', 409)
                if tariff.kind == 'UNIT' and fixed and not is_booking_rate:
                    raise AccessError('Цена кадра закреплена за записью и не меняется при продаже.', 409)
            # Once counts are accepted, use their immutable tariff snapshot.
            if not isinstance(data['price'], str) or (
                not draft.pricing_snapshot and not explicit_tariff
                and data['price'] != await unit_price(session, booking)
            ):
                raise AccessError('Цена пакета изменилась. Обновите данные и проверьте продажу.', 409)
        result = {'draftId': draft.id, 'bookingId': booking.id}
        if kind == 'sale_complete' and draft.status == 'COMPLETED':
            saved = await session.scalar(select(Sale).where(Sale.source_draft_id == draft.id))
            if saved is None:
                raise AccessError('Не найдена оформленная продажа. Нужна проверка владельца.', 409)
            return result | {'saleId': saved.id, 'amount': str(money(saved.amount)), 'soldPhotos': saved.sold_photos,
                'paymentStatus': saved.payment_status, 'draftStatus': draft.status,
                'selectedPhotos': await selected_count(session, draft.id), 'duplicate': True}
        if kind == 'sale_receipt':
            file_id, unique_id = prepared_media['file'] if prepared_media is not None else await self.save_telegram(actor, raw, receipt=True)
            analysis = prepared_media['analysis'] if prepared_media is not None else await extract_receipt(raw)
            await capture_draft_receipt(session, actor, draft, file_id, unique_id,
                hashlib.sha256(raw).hexdigest(), analysis)
            from .services.automatic_receipts import enabled
            if not enabled():
                from .operational_alerts import queue_receipt_review
                await queue_receipt_review(session, booking, actor.id, analysis=analysis, expected=None,
                                           event_key='receipt-review:draft:' + str(draft.id))
        elif kind == 'sale_counts':
            amount = await set_sale_counts(session, actor, draft, data['total'], data['sold'],
                tariff_id=data.get('tariffId'), discount_id=data.get('discountId'),
                album_id=data.get('albumId'), album_quantity=data.get('albumQuantity'),
                photo_amount=data.get('photoAmount'))
            result['amount'] = str(amount)
            from .services.automatic_receipts import enabled
            if draft.receipt_file_id and not enabled():
                from .operational_alerts import queue_receipt_review
                await queue_receipt_review(session, booking, actor.id,
                    analysis=receipt_analysis(draft.receipt_analysis), expected=money(draft.expected_amount),
                    event_key=f'receipt-review:draft:{draft.id}:expected:{money(draft.expected_amount)}')
        elif kind == 'sale_disk_selected':
            from .disk_sale_media import select_photo
            result |= await select_photo(session, actor, booking, draft, data,
                **({'prepared': prepared_media['selection']} if prepared_media is not None else {}))
            # The submitted sale must finish with its last confirmed original,
            # regardless of whether it came from the device or an existing Disk
            # folder. Mobile browsers may stop before sale_complete is sent.
            if draft.status == 'AWAITING_SELECTED' and await selected_count(session, draft.id) == draft.sold_photos:
                complete = await self.complete_prepared_sale(session, actor, draft.id, prepared_media)
                result |= {'saleId': complete.sale.id, 'amount': str(complete.sale_amount),
                           'soldPhotos': complete.sale.sold_photos, 'paymentStatus': complete.sale.payment_status}
        elif kind == 'sale_selected':
            from .services.sale_gallery import link_photo, original_file_reference, storage_location
            if prepared_media is not None and 'gallery_context' in prepared_media:
                _folder, context = await storage_location(session, booking, draft.id)
                if prepared_media['gallery_context'] != context:
                    raise AccessError('Данные съёмки изменились во время загрузки. Повторите сохранение.', 409)
            digest = hashlib.sha256(raw).hexdigest()
            duplicate = await session.scalar(select(SaleDraftPhoto).where(SaleDraftPhoto.draft_id == draft.id, SaleDraftPhoto.sha256 == digest))
            if draft.status == 'COMPLETED' and duplicate:
                return result | {'photoId': duplicate.id, 'duplicate': True, 'draftStatus': draft.status,
                    'selectedPhotos': await selected_count(session, draft.id)}
            if draft.status != 'AWAITING_SELECTED':
                raise AccessError('Сначала сохраните чек и количество кадров.', 409)
            if duplicate:
                path = prepared_media['path'] if prepared_media is not None else await self.save_selected(draft, raw)
                duplicate.storage_path = path
                duplicate.telegram_file_id = original_file_reference(path, digest)
                await link_photo(session, draft, duplicate, verified=True)
                return result | {'photoId': duplicate.id, 'duplicate': True, 'storageVerified': True,
                    'selectedPhotos': await selected_count(session, draft.id),
                    'declaredSold': draft.sold_photos, 'declaredTotal': draft.declared_photo_count,
                    'draftStatus': draft.status}
            if await selected_count(session, draft.id) >= draft.sold_photos:
                raise AccessError('Все выбранные кадры уже сохранены.', 409)
            if await already_purchased_photo(session, booking.id, digest):
                raise AccessError('Этот кадр уже куплен в предыдущей продаже. Выберите новый кадр для допродажи.', 409)
            path = prepared_media['path'] if prepared_media is not None else await self.save_selected(draft, raw)
            # The confirmed private original is sufficient. A second upload to
            # Telegram used to block acceptance after Disk had already stored it.
            photo = SaleDraftPhoto(draft_id=draft.id, telegram_file_id=original_file_reference(path, digest),
                                  telegram_unique_id='web-selected:' + digest,
                                  storage_path=path, sha256=digest, byte_size=len(raw))
            session.add(photo)
            await session.flush()
            await link_photo(session, draft, photo, verified=True)
            result['photoId'] = photo.id
            # The user already submitted the full sale. Finish on the server
            # with the last confirmed original, even if the browser closes
            # before its queued sale_complete request can be transmitted.
            if await selected_count(session, draft.id) == draft.sold_photos:
                complete = await self.complete_prepared_sale(session, actor, draft.id, prepared_media)
                result |= {'saleId': complete.sale.id, 'amount': str(complete.sale_amount),
                           'soldPhotos': complete.sale.sold_photos, 'paymentStatus': complete.sale.payment_status}
        elif kind == 'sale_complete':
            complete = await self.complete_prepared_sale(session, actor, draft.id, prepared_media)
            result |= {'saleId': complete.sale.id, 'amount': str(complete.sale_amount), 'soldPhotos': complete.sale.sold_photos,
                       'paymentStatus': complete.sale.payment_status}
        return result | {'selectedPhotos': await selected_count(session, draft.id),
            'declaredSold': draft.sold_photos, 'declaredTotal': draft.declared_photo_count,
            'draftStatus': draft.status}

    async def receipt(self, session, actor, roles, data, raw, *, prepared_media=None):
        fields(data, 'booking purpose')
        bid = await target(session, actor, data['booking'], 'bookingId')
        if prepared_media is not None and prepared_media['target'] != bid:
            raise AccessError('Запись изменилась. Повторите отправку.', 409)
        booking = await session.get(Booking, bid, with_for_update=True)
        if not await can_access_booking(session, actor.id, roles, booking):
            raise AccessError('Нет доступа к записи.', 403)
        if data['purpose'] not in ('DEPOSIT', 'PAYMENT'):
            raise AccessError('Неизвестный вид чека.', 400)
        from .services.automatic_receipts import (admit, enabled, existing_upload,
                                                  pending_upload, supersede_exhausted)
        if enabled():
            existing = await existing_upload(session, bid, data['purpose'], hashlib.sha256(raw).hexdigest())
            if existing:
                return {'bookingId': bid, 'receiptId': existing.id, 'receipt': receipt_state(existing), 'duplicate': True}
        pending = await pending_upload(session, bid, data['purpose'])
        if pending:
            raise AccessError(f'Чек №{pending} уже ожидает проверки.', 409)
        expected = await expected_amount(session, booking, data['purpose'])
        if expected <= 0:
            raise AccessError('Дополнительная оплата не требуется.', 409)
        file_id, unique_id = prepared_media['file'] if prepared_media is not None else await self.save_telegram(actor, raw, receipt=True)
        analysis = prepared_media['analysis'] if prepared_media is not None else await extract_receipt(raw)
        from .services.receipt_documents import manifest
        analysis = dict(analysis, documents=manifest(raw))
        receipt = Receipt(booking_id=bid, uploaded_by_id=actor.id, purpose=data['purpose'],
            file_id=file_id, file_unique_id=unique_id, image_sha256=hashlib.sha256(raw).hexdigest(),
            operation_key=operation_key(analysis.get('fields', {})), analysis=json.dumps(analysis, ensure_ascii=False),
            expected_amount=expected, status='PENDING')
        session.add(receipt)
        from .booking_refund_integration import bind_new_receipt
        await bind_new_receipt(session, receipt, booking)
        await session.flush()
        if enabled():
            await supersede_exhausted(session, bid, data['purpose'], receipt.id)
            await admit(session, booking, receipt)
        else:
            from .operational_alerts import queue_receipt_review
            await queue_receipt_review(session, booking, actor.id, analysis=analysis, expected=expected,
                                       event_key='receipt-review:receipt:' + str(receipt.id))
        await audit(session, actor, 'receipt_uploaded', 'receipt', receipt.id)
        return {'bookingId': bid, 'receiptId': receipt.id, 'receipt': receipt_state(receipt)}

    async def link_selected_original(self, session, actor, booking, shooting, data):
        """Count a guest-selected original in the full shoot without a second upload.

        The selected original of this same booking is already stored and
        checksum-verified on Disk. Reuse that exact file; never another guest's.
        """
        sid, digest = shooting.id, data['sha256']
        name = original_name(data.get('filename'))
        existing = await session.scalar(select(PhotoStorage).where(
            PhotoStorage.shooting_id == sid, PhotoStorage.sha256 == digest).limit(1))
        if existing is not None:
            photo = await session.get(Photo, existing.photo_id)
            if name and photo is not None and not photo.original_name:
                photo.original_name = name
            return {'shootingId': sid, 'photoId': existing.photo_id, 'duplicate': True,
                    'storageStatus': existing.status, 'linked': True}
        if shooting.status != 'READY_FOR_SALE' or shooting.full_upload_completed_at:
            raise AccessError('Загрузка новых кадров уже закрыта или ещё недоступна.', 409)
        from .disk_sale_media import FILE_PREFIX
        selected = await session.scalar(select(SaleDraftPhoto).join(
            SaleDraft, SaleDraft.id == SaleDraftPhoto.draft_id).where(
            SaleDraft.booking_id == booking.id, SaleDraftPhoto.sha256 == digest,
            ~SaleDraftPhoto.storage_path.like(FILE_PREFIX + '%')).order_by(SaleDraftPhoto.id).limit(1))
        if selected is None:
            # The browser falls back to a normal upload of this file.
            raise AccessError('Кадр не найден среди выбранных. Загрузите файл обычным способом.', 409)
        path = selected.storage_path
        photo = Photo(shooting_id=sid, file_id=path, original_name=name)
        session.add(photo)
        await session.flush()
        storage, _ = await ensure_photo_storage(session, photo_id=photo.id, shooting_id=sid,
            file_id=path, file_unique_id='web-sha256:' + digest, source_kind='DOCUMENT')
        storage.telegram_file_id = storage.disk_path = path
        storage.sha256, storage.byte_size = digest, selected.byte_size
        storage.status, storage.attempts, storage.last_error = 'STORED', 0, None
        storage.stored_at = storage.updated_at = utc_now()
        storage.started_at = None
        await audit(session, actor, 'shoot_photo_uploaded', 'shooting', sid,
                    f'linked_selected_original; sale_draft_photo={selected.id}')
        return {'shootingId': sid, 'photoId': photo.id, 'duplicate': False,
                'storageStatus': 'STORED', 'storageVerified': True, 'linked': True}

    async def shoot(self, session, actor, roles, kind, data, raw, *, prepared_media=None):
        if kind in {'shoot_photo', 'shoot_photo_link'}:
            shoot_fields(kind, data)
        else:
            fields(data, 'shooting')
        sid = await target(session, actor, data['shooting'], 'shootingId')
        if prepared_media is not None and prepared_media['target'] != sid:
            raise AccessError('Съёмка изменилась. Повторите отправку.', 409)
        preview = await session.get(Shooting, sid)
        booking = await session.get(Booking, preview.booking_id, with_for_update=True) if preview else None
        shooting = await session.get(Shooting, sid, with_for_update=True, populate_existing=True) if preview else None
        if not await can_access_booking(session, actor.id, roles, booking, photos=True):
            raise AccessError('Нет доступа к съёмке.', 403)
        if kind == 'shoot_retry':
            from .services.photo_storage import repair_storage_queue
            recovery = await repair_storage_queue(session, sid)
            await audit(session, actor, 'shoot_storage_recovery', 'shooting', sid,
                        json.dumps(recovery))
            return {'shootingId': sid, **recovery}
        if kind == 'shoot_photo_link':
            return await self.link_selected_original(session, actor, booking, shooting, data)
        if kind == 'shoot_photo':
            from .services.media_files import direct_disk_mode
            digest = hashlib.sha256(raw).hexdigest()
            duplicate = await session.scalar(select(PhotoStorage).where(PhotoStorage.shooting_id == sid, PhotoStorage.sha256 == digest).limit(1))
            if direct_disk_mode(getattr(self.api, 'standalone', None)):
                if not duplicate and (shooting.status != 'READY_FOR_SALE' or shooting.full_upload_completed_at):
                    raise AccessError('Загрузка новых кадров уже закрыта или ещё недоступна.', 409)
                path = prepared_media['path'] if prepared_media is not None else await self.save_disk_original(f'/shootings/{sid}', raw)
                if duplicate:
                    photo = await session.get(Photo, duplicate.photo_id)
                    storage = duplicate
                else:
                    photo = Photo(shooting_id=sid, file_id=path)
                    session.add(photo)
                    await session.flush()
                    storage, _ = await ensure_photo_storage(session, photo_id=photo.id, shooting_id=sid,
                        file_id=path, file_unique_id='web-sha256:' + digest, source_kind='DOCUMENT')
                photo.file_id = storage.telegram_file_id = storage.disk_path = path
                photo.original_name = original_name(data.get('filename')) or photo.original_name
                storage.sha256, storage.byte_size = digest, len(raw)
                storage.status, storage.attempts, storage.last_error = 'STORED', 0, None
                storage.stored_at = storage.updated_at = utc_now()
                storage.started_at = None
                await audit(session, actor, 'shoot_photo_uploaded', 'shooting', sid, 'direct_disk; checksum verified')
                return {'shootingId': sid, 'photoId': photo.id, 'duplicate': bool(duplicate),
                        'storageStatus': 'STORED', 'storageVerified': True}
            if duplicate:
                file_id, unique_id = prepared_media['file'] if prepared_media is not None else await self.save_telegram(actor, raw)
                original = await session.get(Photo, duplicate.photo_id)
                original.file_id = file_id
                duplicate.telegram_file_id = file_id
                duplicate.telegram_unique_id = unique_id
                duplicate.status, duplicate.attempts, duplicate.last_error = 'PENDING', 0, None
                duplicate.started_at = None
                return {'shootingId': sid, 'photoId': duplicate.photo_id, 'duplicate': True, 'storageStatus': 'PENDING'}
            if shooting.status != 'READY_FOR_SALE' or shooting.full_upload_completed_at:
                raise AccessError('Загрузка новых кадров уже закрыта или ещё недоступна.', 409)
            file_id, unique_id = prepared_media['file'] if prepared_media is not None else await self.save_telegram(actor, raw)
            photo = Photo(shooting_id=sid, file_id=file_id, original_name=original_name(data.get('filename')))
            session.add(photo)
            await session.flush()
            storage, _ = await ensure_photo_storage(session, photo_id=photo.id, shooting_id=sid,
                file_id=file_id, file_unique_id=unique_id, source_kind='DOCUMENT')
            storage.sha256 = digest
            await audit(session, actor, 'shoot_photo_uploaded', 'shooting', sid)
            return {'shootingId': sid, 'photoId': photo.id, 'storageStatus': 'PENDING'}
        if shooting.status != 'READY_FOR_SALE' or shooting.full_upload_completed_at:
            raise AccessError('Загрузка съёмки уже закрыта или ещё недоступна.', 409)
        completed = await complete_full_upload(session, actor, sid, allow_management=True)
        return {'shootingId': sid, 'photos': completed.count, 'percent': str(completed.commissions['percent'])}

