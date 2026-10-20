"""Mini App adapter for existing booking, sale and photo services.

OperationRequest and the caller's user lock provide replay protection. This
module never commits independently of that receipt of the operation.
"""
import hashlib
import json
from decimal import Decimal

from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .bank_payments import configured as bank_configured
from .miniapp_security import AccessError
from .models import (
    BankPayment,
    Booking,
    Client,
    Hotel,
    HotelTariff,
    OperationRequest,
    Package,
    Photo,
    PhotoEdit,
    PhotoStorage,
    Receipt,
    Sale,
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

KINDS = frozenset({'booking', 'sale_start', 'sale_counts', 'sale_complete', 'sale_receipt',
                   'sale_selected', 'booking_receipt', 'shoot_photo', 'shoot_complete', 'shoot_retry', 'photo_edit', 'delivery_photo', 'delivery_handoff'})
MEDIA_KINDS = frozenset({'sale_receipt', 'sale_selected', 'booking_receipt', 'shoot_photo', 'delivery_photo'})


def fields(data, expected):
    if set(data) != set(expected.split()):
        raise AccessError('Некорректные поля рабочей операции.', 400)


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
    return {'id': receipt.id, 'status': receipt.status, 'source': analysis.get('provider'),
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

    async def receipt_image(self, request):
        return await self._receipt_image(request, draft=False)

    async def draft_receipt_image(self, request):
        return await self._receipt_image(request, draft=True)

    async def _receipt_image(self, request, *, draft):
        from .people import positive_id
        from .services.media_files import load_private_media
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
        raw = await load_private_media(self.api.bot, file_id, max_bytes=8 * 1024 * 1024)
        return web.Response(body=raw, content_type=image_format(raw)[1], headers={
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
                edits = (await session.scalars(select(PhotoEdit).join(PhotoStorage, PhotoStorage.photo_id == PhotoEdit.photo_id).where(PhotoStorage.shooting_id == shoot.id).order_by(PhotoEdit.id))).all() if shoot else []
                purchases = (await session.scalars(select(Sale).where(Sale.booking_id == b.id).order_by(Sale.id))).all()
                receipts = (await session.scalars(select(Receipt).where(Receipt.booking_id == b.id).order_by(Receipt.id))).all()
                can_upload = await can_access_booking(session, a['id'], set(a['roles']), b, photos=True)
                customer = await session.get(Client, b.client_id)
                hotel = await session.get(Hotel, b.hotel_id)
                total, paid, due = await payment_totals(session, b.id)
                original_count = await session.scalar(select(func.count(Photo.id)).where(Photo.shooting_id == shoot.id)) if shoot else 0
                draft_actor = await session.get(User, draft.created_by_id) if draft else None
                history = {'client': customer.name if customer else '', 'phone': customer.phone if customer else None,
                           'hotel': hotel.name if hotel else 'Отель не указан', 'sales': [
                    {'id': sale.id, 'at': sale.created_at.isoformat() + 'Z', 'photos': sale.sold_photos,
                     'amount': str(money(sale.amount)), 'paymentStatus': sale.payment_status,
                     'lines': (snapshot(sale.pricing_snapshot) or {}).get('lines', [])}
                    for sale in purchases], 'soldTotal': sum(sale.sold_photos for sale in purchases),
                    'salesAmount': str(total), 'paidAmount': str(paid), 'dueAmount': str(due),
                    'totalFrames': max([original_count or 0, draft.declared_photo_count or 0 if draft else 0] + [sale.declared_photo_count or 0 for sale in purchases])}
                rows.append({**history, 'receipts': [receipt_state(receipt) for receipt in receipts], 'edits': [{'id': e.id, 'photo': e.photo_id, 'status': e.status, 'error': e.last_error} for e in edits], 'files': [{'id': f.photo_id, 'status': f.status, 'error': (('Хранилище заполнено. Освободите место и повторите.' if 'заполнено' in f.last_error else 'Не удалось сохранить. Повторите загрузку.') if f.last_error else None)} for f in files], 'id': b.id, 'date': str(b.shoot_date), 'time': str(b.shoot_time)[:5],
                    'room': b.room, 'status': b.status, 'hotelId': b.hotel_id, 'price': effective_price,
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
                            (str(money(Decimal(effective_price) * draft.sold_photos)) if draft.sold_photos else None)),
                        'expectedAmount': (str(money(max(Decimal(0), total + Decimal(
                            (snapshot(draft.pricing_snapshot) or {}).get('amount') or
                            str(money(Decimal(effective_price) * draft.sold_photos))) - paid)))
                            if draft.sold_photos else None),
                        'receiptStored': bool(draft.receipt_file_id),
                        'receiptImagePath': (f'/sale-drafts/{draft.id}/receipt/image'
                            if draft.receipt_file_id and (draft.created_by_id == a['id'] or {'OWNER', 'ADMIN'} & set(a['roles'])) else None),
                        'receiptAnalysisStatus': receipt_analysis(draft.receipt_analysis).get('status', 'unavailable'),
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
        return web.json_response({'bookings': rows, 'hotels': [{'id': h.id, 'name': h.name} for h in hotels],
            'tariffs': [{'id': t.id, 'hotelId': t.hotel_id, 'name': t.name, 'kind': t.kind,
                        'price': str(money(t.price)), 'photoCount': t.photo_count} for t in tariffs],
            'canConfigurePricing': bool({'OWNER', 'ADMIN'} & set(a['roles'])),
            'bankConfigured': bank_configured(),
            'packages': [{'id': p.id, 'name': p.name, 'price': prices[p.id]} for p in packages if p.active],
            'canBook': bool({'OWNER', 'ADMIN', 'MANAGER'} & set(a['roles'])),
            'date': str(self.api.today()), 'actorId': a['id']})

    async def save_telegram(self, actor, raw, *, receipt=False):
        from .push_notifications import app_channel
        if app_channel():
            from .services.media_files import save_private_media
            return await save_private_media(actor.id, raw)
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
            return await save_private_media(actor.id, raw)

    async def save_selected(self, draft, raw):
        return await self.save_disk_original(f'/sales/draft-{draft.id}/selected', raw)

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

    async def apply(self, conn, actor_data, kind, data, raw=None):
        if kind in MEDIA_KINDS:
            if not raw or len(raw) < 100:
                raise AccessError('Прикрепите изображение.', 400)
            try:
                image_format(raw)
            except ValueError as exc:
                raise AccessError('Нужен поддерживаемый формат изображения: JPEG или PNG.',400) from exc
            if kind in {'sale_receipt', 'booking_receipt'} and len(raw) > 8 * 1024 * 1024:
                raise AccessError('Чек должен быть не больше 8 МБ.', 413)
        elif raw is not None:
            raise AccessError('В этой операции не должно быть вложения.', 400)
        try:
            async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                actor = await session.get(User, actor_data['id'])
                roles = set(actor_data['roles'])
                if kind == 'booking':
                    fields(data, 'client_name client_phone hotel_id package_id room guest_count deposit shoot_date shoot_time photographer_id')
                    if data['photographer_id'] is not None and type(data['photographer_id']) is not int:
                        raise AccessError('Некорректный фотограф.', 400)
                    booking = await create_booking_record(session, actor, data, data['photographer_id'])
                    result = {'bookingId': booking.id}
                elif kind.startswith('delivery_'):
                    from .delivery import Delivery
                    result = await Delivery(self.api).apply(session, actor_data, kind, data, raw)
                elif kind == 'photo_edit':
                    from .photo_edits import apply
                    result = await apply(session, actor_data, data)
                elif kind.startswith('sale_'):
                    result = await self.sale(session, actor, roles, kind, data, raw)
                elif kind == 'booking_receipt':
                    result = await self.receipt(session, actor, roles, data, raw)
                else:
                    result = await self.shoot(session, actor, roles, kind, data, raw)
                await session.flush()
                # Bound to the outer transaction; no independent financial commit.
                return result
        except AccessError:
            raise
        except StorageConfirmationPending as exc:
            raise AccessError(str(exc), 424) from exc
        except ValueError as exc:
            raise AccessError(str(exc), 409) from exc
        except YandexDiskError as exc:
            raise AccessError('Яндекс.Диск временно недоступен. Вложение сохранено локально.', 503) from exc

    async def sale(self, session, actor, roles, kind, data, raw):
        if kind == 'sale_start':
            fields(data, 'booking price')
            bid = await target(session, actor, data['booking'], 'bookingId')
            draft = await start_sale_draft(session, actor, roles, bid)
        else:
            expected = {'sale_counts': 'draft total sold price', 'sale_complete': 'draft price',
                        'sale_receipt': 'draft', 'sale_selected': 'draft'}[kind]
            fields(data, expected + (' tariffId' if kind == 'sale_counts' and 'tariffId' in data else ''))
            did = await target(session, actor, data['draft'], 'draftId')
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
            # Once counts are accepted, use their immutable tariff snapshot.
            if not isinstance(data['price'], str) or (not draft.pricing_snapshot and data['price'] != await unit_price(session, booking)):
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
            file_id, unique_id = await self.save_telegram(actor, raw, receipt=True)
            await capture_draft_receipt(session, actor, draft, file_id, unique_id,
                hashlib.sha256(raw).hexdigest(), await extract_receipt(raw))
        elif kind == 'sale_counts':
            amount = await set_sale_counts(session, actor, draft, data['total'], data['sold'], tariff_id=data.get('tariffId'))
            result['amount'] = str(amount)
        elif kind == 'sale_selected':
            digest = hashlib.sha256(raw).hexdigest()
            duplicate = await session.scalar(select(SaleDraftPhoto).where(SaleDraftPhoto.draft_id == draft.id, SaleDraftPhoto.sha256 == digest))
            if draft.status == 'COMPLETED' and duplicate:
                return result | {'photoId': duplicate.id, 'duplicate': True, 'draftStatus': draft.status,
                    'selectedPhotos': await selected_count(session, draft.id)}
            if draft.status != 'AWAITING_SELECTED':
                raise AccessError('Сначала сохраните чек и количество кадров.', 409)
            if duplicate:
                path = await self.save_selected(draft, raw)
                duplicate.storage_path = path
                duplicate.telegram_file_id = path
                return result | {'photoId': duplicate.id, 'duplicate': True, 'storageVerified': True,
                    'selectedPhotos': await selected_count(session, draft.id),
                    'declaredSold': draft.sold_photos, 'declaredTotal': draft.declared_photo_count,
                    'draftStatus': draft.status}
            if await selected_count(session, draft.id) >= draft.sold_photos:
                raise AccessError('Все выбранные кадры уже сохранены.', 409)
            if await already_purchased_photo(session, booking.id, digest):
                raise AccessError('Этот кадр уже куплен в предыдущей продаже. Выберите новый кадр для допродажи.', 409)
            path = await self.save_selected(draft, raw)
            # The confirmed private original is sufficient. A second upload to
            # Telegram used to block acceptance after Disk had already stored it.
            photo = SaleDraftPhoto(draft_id=draft.id, telegram_file_id=path,
                                  telegram_unique_id='web-selected:' + digest,
                                  storage_path=path, sha256=digest, byte_size=len(raw))
            session.add(photo)
            await session.flush()
            result['photoId'] = photo.id
            # The user already submitted the full sale. Finish on the server
            # with the last confirmed original, even if the browser closes
            # before its queued sale_complete request can be transmitted.
            if await selected_count(session, draft.id) == draft.sold_photos:
                complete = await complete_sale(session, actor, draft.id)
                result |= {'saleId': complete.sale.id, 'amount': str(complete.sale_amount),
                           'soldPhotos': complete.sale.sold_photos, 'paymentStatus': complete.sale.payment_status}
        elif kind == 'sale_complete':
            complete = await complete_sale(session, actor, draft.id)
            result |= {'saleId': complete.sale.id, 'amount': str(complete.sale_amount), 'soldPhotos': complete.sale.sold_photos,
                       'paymentStatus': complete.sale.payment_status}
        return result | {'selectedPhotos': await selected_count(session, draft.id),
            'declaredSold': draft.sold_photos, 'declaredTotal': draft.declared_photo_count,
            'draftStatus': draft.status}

    async def receipt(self, session, actor, roles, data, raw):
        fields(data, 'booking purpose')
        bid = await target(session, actor, data['booking'], 'bookingId')
        booking = await session.get(Booking, bid, with_for_update=True)
        if not await can_access_booking(session, actor.id, roles, booking):
            raise AccessError('Нет доступа к записи.', 403)
        if data['purpose'] not in ('DEPOSIT', 'PAYMENT'):
            raise AccessError('Неизвестный вид чека.', 400)
        pending = await session.scalar(select(Receipt.id).where(Receipt.booking_id == bid,
            Receipt.purpose == data['purpose'], Receipt.status == 'PENDING'))
        if pending:
            raise AccessError(f'Чек №{pending} уже ожидает проверки.', 409)
        expected = await expected_amount(session, booking, data['purpose'])
        if expected <= 0:
            raise AccessError('Дополнительная оплата не требуется.', 409)
        file_id, unique_id = await self.save_telegram(actor, raw, receipt=True)
        analysis = await extract_receipt(raw)
        receipt = Receipt(booking_id=bid, uploaded_by_id=actor.id, purpose=data['purpose'],
            file_id=file_id, file_unique_id=unique_id, image_sha256=hashlib.sha256(raw).hexdigest(),
            operation_key=operation_key(analysis.get('fields', {})), analysis=json.dumps(analysis, ensure_ascii=False),
            expected_amount=expected, status='PENDING')
        session.add(receipt)
        await session.flush()
        await audit(session, actor, 'receipt_uploaded', 'receipt', receipt.id)
        return {'bookingId': bid, 'receiptId': receipt.id}

    async def shoot(self, session, actor, roles, kind, data, raw):
        fields(data, 'shooting')
        sid = await target(session, actor, data['shooting'], 'shootingId')
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
        if kind == 'shoot_photo':
            from .push_notifications import app_channel
            digest = hashlib.sha256(raw).hexdigest()
            duplicate = await session.scalar(select(PhotoStorage).where(PhotoStorage.shooting_id == sid, PhotoStorage.sha256 == digest).limit(1))
            if app_channel():
                if not duplicate and (shooting.status != 'READY_FOR_SALE' or shooting.full_upload_completed_at):
                    raise AccessError('Загрузка новых кадров уже закрыта или ещё недоступна.', 409)
                path = await self.save_disk_original(f'/shootings/{sid}', raw)
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
                storage.sha256, storage.byte_size = digest, len(raw)
                storage.status, storage.attempts, storage.last_error = 'STORED', 0, None
                storage.stored_at = storage.updated_at = utc_now()
                storage.started_at = None
                await audit(session, actor, 'shoot_photo_uploaded', 'shooting', sid, 'direct_disk; checksum verified')
                return {'shootingId': sid, 'photoId': photo.id, 'duplicate': bool(duplicate),
                        'storageStatus': 'STORED', 'storageVerified': True}
            if duplicate:
                file_id, unique_id = await self.save_telegram(actor, raw)
                original = await session.get(Photo, duplicate.photo_id)
                original.file_id = file_id
                duplicate.telegram_file_id = file_id
                duplicate.telegram_unique_id = unique_id
                duplicate.status, duplicate.attempts, duplicate.last_error = 'PENDING', 0, None
                duplicate.started_at = None
                return {'shootingId': sid, 'photoId': duplicate.photo_id, 'duplicate': True, 'storageStatus': 'PENDING'}
            if shooting.status != 'READY_FOR_SALE' or shooting.full_upload_completed_at:
                raise AccessError('Загрузка новых кадров уже закрыта или ещё недоступна.', 409)
            file_id, unique_id = await self.save_telegram(actor, raw)
            photo = Photo(shooting_id=sid, file_id=file_id)
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

