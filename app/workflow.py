"""Mini App adapter for existing booking, sale and photo services.

OperationRequest and the caller's user lock provide replay protection. This
module never commits independently of that receipt of the operation.
"""
import hashlib
import json

from aiohttp import web
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError, booking_assignment_columns
from .models import (
    Booking,
    Hotel,
    OperationRequest,
    Package,
    Photo,
    PhotoStorage,
    Receipt,
    SaleDraft,
    SaleDraftPhoto,
    Shooting,
    User,
)
from .services.bookings import create_booking_record
from .services.commissions import booking_photo_price
from .services.core import audit
from .services.photo_storage import ensure_photo_storage, image_format
from .services.receipts import expected_amount, extract_receipt, money, operation_key
from .services.sale_workflow import (
    can_sell,
    capture_draft_receipt,
    complete_full_upload,
    complete_sale,
    selected_count,
    set_sale_counts,
    start_sale_draft,
)
from .yandex_disk import ROOT, YandexDisk, YandexDiskError, configured_from_env

KINDS = frozenset({'booking', 'sale_start', 'sale_counts', 'sale_complete', 'sale_receipt',
                   'sale_selected', 'booking_receipt', 'shoot_photo', 'shoot_complete'})
MEDIA_KINDS = frozenset({'sale_receipt', 'sale_selected', 'booking_receipt', 'shoot_photo'})


def fields(data, expected):
    if set(data) != set(expected.split()):
        raise AccessError('Некорректные поля рабочей операции.', 400)


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

    async def listing(self, request):
        a = request['miniapp_actor']
        async with AsyncSession(self.api.engine) as session:
            query = select(Booking).where(Booking.status.notin_(['CANCELLED', 'REJECTED']))
            if not {'OWNER', 'ADMIN'} & set(a['roles']):
                assignments = booking_assignment_columns(a['roles'])
                query = query.where(or_(False, *(getattr(Booking, column) == a['id'] for column in assignments)))
            bookings = (await session.scalars(query.order_by(Booking.shoot_date.desc()).limit(200))).all()
            packages = (await session.scalars(select(Package))).all()
            prices = {p.id: str(money(p.price_per_photo)) for p in packages}
            rows = []
            for b in bookings:
                draft = await session.scalar(select(SaleDraft).where(SaleDraft.booking_id == b.id,
                    SaleDraft.status.in_(['AWAITING_RECEIPT', 'AWAITING_COUNTS', 'AWAITING_SELECTED'])).order_by(SaleDraft.id.desc()).limit(1))
                shoot = await session.scalar(select(Shooting).where(Shooting.booking_id == b.id))
                rows.append({'id': b.id, 'date': str(b.shoot_date), 'time': str(b.shoot_time)[:5],
                    'room': b.room, 'status': b.status, 'hotelId': b.hotel_id, 'price': str(money(draft.unit_price if draft and draft.unit_price else await booking_photo_price(session, b))),
                    'shootingId': shoot.id if shoot else None, 'fullUploaded': bool(shoot and shoot.full_upload_completed_at),
                    'canUpload': ('PHOTOGRAPHER' in a['roles'] and b.photographer_id == a['id']) or bool({'OWNER', 'ADMIN'} & set(a['roles'])),
                    'draft': ({'id': draft.id, 'mine': draft.created_by_id == a['id'], 'status': draft.status,
                        'total': draft.declared_photo_count, 'sold': draft.sold_photos, 'discount': str(draft.discount_percent or 0),
                        'selected': await selected_count(session, draft.id)} if draft else None)})
            hotels = (await session.scalars(select(Hotel).where(Hotel.active.is_(True)))).all()
        return web.json_response({'bookings': rows, 'hotels': [{'id': h.id, 'name': h.name} for h in hotels],
            'packages': [{'id': p.id, 'name': p.name, 'price': prices[p.id]} for p in packages if p.active],
            'canBook': bool({'OWNER', 'ADMIN', 'MANAGER'} & set(a['roles'])),
            'date': str(self.api.today()), 'actorId': a['id']})

    async def save_telegram(self, actor, raw, *, receipt=False):
        from aiogram.types import BufferedInputFile
        ext, _ = image_format(raw)
        upload = BufferedInputFile(raw, filename=f'photo-boss.{ext}')
        if receipt:
            message = await self.api.bot.send_photo(actor.tg_id, upload, caption='Photo Boss · чек на проверку', protect_content=True)
            attachment = message.photo[-1]
        else:
            message = await self.api.bot.send_document(actor.tg_id, upload, caption='Photo Boss · сохранённый кадр', protect_content=True)
            attachment = message.document
        return attachment.file_id, attachment.file_unique_id

    async def save_selected(self, draft, raw):
        token, client_id = configured_from_env()
        if not token:
            raise AccessError('Яндекс.Диск не подключён. Фото остаётся в локальной очереди.', 503)
        ext, mime = image_format(raw)
        digest = hashlib.sha256(raw).hexdigest()
        storage = YandexDisk(token, client_id)
        for path in (ROOT + '/sales', ROOT + f'/sales/draft-{draft.id}', ROOT + f'/sales/draft-{draft.id}/selected'):
            await storage.ensure_dir(path)
        path = ROOT + f'/sales/draft-{draft.id}/selected/{digest}.{ext}'
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
            if kind == 'sale_counts' and 'discount' in data:
                expected += ' discount'
            fields(data, expected)
            did = await target(session, actor, data['draft'], 'draftId')
            preview = await session.get(SaleDraft, did)
            if preview is None:
                raise AccessError('Черновик не найден.', 404)
            await session.get(Booking, preview.booking_id, with_for_update=True)
            draft = await session.get(SaleDraft, did, with_for_update=True, populate_existing=True)
        booking = await session.get(Booking, draft.booking_id)
        if draft.created_by_id != actor.id or not await can_sell(actor, roles, booking):
            raise AccessError('Нет доступа к этой продаже.', 403)
        if 'price' in data:
            price = draft.unit_price or await booking_photo_price(session, booking)
            if not isinstance(data['price'], str) or data['price'] != str(money(price)):
                raise AccessError('Цена кадра изменилась. Обновите данные и проверьте продажу.', 409)
        result = {'draftId': draft.id, 'bookingId': booking.id}
        if kind == 'sale_receipt':
            file_id, unique_id = await self.save_telegram(actor, raw, receipt=True)
            await capture_draft_receipt(session, actor, draft, file_id, unique_id,
                hashlib.sha256(raw).hexdigest(), await extract_receipt(raw))
        elif kind == 'sale_counts':
            amount = await set_sale_counts(session, actor, draft, data['total'], data['sold'], data.get('discount', 0))
            result['amount'] = str(amount)
        elif kind == 'sale_selected':
            if draft.status != 'AWAITING_SELECTED':
                raise AccessError('Сначала сохраните чек и количество кадров.', 409)
            digest = hashlib.sha256(raw).hexdigest()
            duplicate = await session.scalar(select(SaleDraftPhoto.id).where(SaleDraftPhoto.draft_id == draft.id, SaleDraftPhoto.sha256 == digest))
            if duplicate:
                return result | {'photoId': duplicate, 'duplicate': True}
            if await selected_count(session, draft.id) >= draft.sold_photos:
                raise AccessError('Все выбранные кадры уже сохранены.', 409)
            path = await self.save_selected(draft, raw)
            file_id, unique_id = await self.save_telegram(actor, raw)
            photo = SaleDraftPhoto(draft_id=draft.id, telegram_file_id=file_id, telegram_unique_id=unique_id,
                                  storage_path=path, sha256=digest, byte_size=len(raw))
            session.add(photo)
            await session.flush()
            result['photoId'] = photo.id
        elif kind == 'sale_complete':
            complete = await complete_sale(session, actor, draft.id)
            result |= {'saleId': complete.sale.id, 'amount': str(complete.sale_amount),
                       'paymentStatus': complete.sale.payment_status}
        return result

    async def receipt(self, session, actor, roles, data, raw):
        fields(data, 'booking purpose')
        bid = await target(session, actor, data['booking'], 'bookingId')
        booking = await session.get(Booking, bid, with_for_update=True)
        if booking is None or (not roles & {'OWNER', 'ADMIN'} and not any(
            getattr(booking, column) == actor.id for column in booking_assignment_columns(roles)
        )):
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
        shooting = await session.get(Shooting, sid, with_for_update=True)
        booking = await session.get(Booking, shooting.booking_id) if shooting else None
        if booking is None or (not roles & {'OWNER', 'ADMIN'} and (
            'PHOTOGRAPHER' not in roles or booking.photographer_id != actor.id
        )):
            raise AccessError('Нет доступа к съёмке.', 403)
        if shooting.status != 'READY_FOR_SALE' or shooting.full_upload_completed_at:
            raise AccessError('Загрузка съёмки уже закрыта или ещё недоступна.', 409)
        if kind == 'shoot_photo':
            digest = hashlib.sha256(raw).hexdigest()
            duplicate = await session.scalar(select(PhotoStorage.photo_id).where(PhotoStorage.shooting_id == sid, PhotoStorage.sha256 == digest).limit(1))
            if duplicate:
                return {'shootingId': sid, 'photoId': duplicate, 'duplicate': True}
            file_id, unique_id = await self.save_telegram(actor, raw)
            photo = Photo(shooting_id=sid, file_id=file_id)
            session.add(photo)
            await session.flush()
            storage, _ = await ensure_photo_storage(session, photo_id=photo.id, shooting_id=sid,
                file_id=file_id, file_unique_id=unique_id, source_kind='DOCUMENT')
            storage.sha256 = digest
            await audit(session, actor, 'shoot_photo_uploaded', 'shooting', sid)
            return {'shootingId': sid, 'photoId': photo.id}
        completed = await complete_full_upload(session, actor, sid, allow_management=True)
        return {'shootingId': sid, 'photos': completed.count, 'percent': str(completed.commissions['percent'])}
