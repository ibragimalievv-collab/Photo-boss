import hashlib
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from ..access import StaffFilter
from ..config import config, number
from ..db import Session
from ..keyboards import inline
from ..models import (
    Booking,
    Package,
    PayrollEntry,
    Receipt,
    Sale,
    SaleDraft,
    SaleDraftPhoto,
    Shooting,
    User,
)
from ..services.core import audit, setting
from ..services.photo_storage import (
    MAX_TELEGRAM_IMAGE_BYTES,
    LimitedBuffer,
    image_format,
)
from ..services.receipts import (
    MAX_IMAGE_BYTES,
    download_receipt,
    extract_receipt,
    money,
    operation_key,
    payment_totals,
    refresh_payment_statuses,
)
from ..services.sale_workflow import final_photographer_percent
from ..yandex_disk import ROOT, YandexDisk, YandexDiskError, configured_from_env

r = Router()
r.message.filter(StaffFilter("PHOTOGRAPHER", "MANAGER"))
r.callback_query.filter(StaffFilter("PHOTOGRAPHER", "MANAGER"))


class S(StatesGroup):
    booking = State()
    receipt = State()
    total = State()
    sold = State()
    selected = State()


ACTIVE_DRAFT_STATUSES = (
    "AWAITING_RECEIPT",
    "AWAITING_COUNTS",
    "AWAITING_SELECTED",
)


def positive_id(value):
    try:
        result = int(value)
        return result if 0 < result <= 2**31 - 1 else None
    except (TypeError, ValueError):
        return None


async def can_sell(actor, roles, booking):
    if booking is None or booking.status != "READY_FOR_SALE":
        return False
    if roles & {"OWNER", "ADMIN"}:
        return True
    if "MANAGER" in roles and booking.manager_id == actor.id:
        return True
    return "PHOTOGRAPHER" in roles and booking.photographer_id == actor.id


async def ready_bookings(session, actor, roles):
    query = select(Booking).where(Booking.status == "READY_FOR_SALE")
    if not roles & {"OWNER", "ADMIN"}:
        query = query.where(
            or_(
                Booking.manager_id == actor.id,
                Booking.photographer_id == actor.id,
            )
        )
    return (
        await session.scalars(
            query.order_by(Booking.shoot_date.desc(), Booking.shoot_time.desc()).limit(30)
        )
    ).all()


async def active_draft(session, booking_id):
    return await session.scalar(
        select(SaleDraft)
        .where(
            SaleDraft.booking_id == booking_id,
            SaleDraft.status.in_(ACTIVE_DRAFT_STATUSES),
        )
        .order_by(SaleDraft.id.desc())
        .limit(1)
    )


async def selected_count(session, draft_id):
    return int(
        await session.scalar(
            select(func.count(SaleDraftPhoto.id)).where(
                SaleDraftPhoto.draft_id == draft_id
            )
        )
        or 0
    )


async def continue_draft(message, state, draft):
    if draft.status == "AWAITING_RECEIPT":
        await state.set_state(S.receipt)
        await state.set_data({"sale_draft_id": draft.id})
        return await message.answer(
            f"🧾 Продажа · запись #{draft.booking_id}\n\n"
            "Сначала отправьте одно фото чека продажи. "
            "Photo Boss проверит изображение и сохранит его для проверки владельцем.\n"
            "Отмена: /cancel"
        )
    if draft.status == "AWAITING_COUNTS":
        await state.set_state(S.total)
        await state.set_data({"sale_draft_id": draft.id})
        return await message.answer(
            "Введите ОБЩЕЕ количество кадров в этой фотосессии.\n"
            "Это контрольное число. Процент фотографа по нему не рассчитывается — "
            "ставка фиксируется только после фактической загрузки всей съёмки."
        )
    await state.set_state(S.selected)
    await state.set_data({"sale_draft_id": draft.id})
    count = await selected_count_from_id(draft.id)
    return await message.answer(
        f"Загрузите выбранные фотографии: {count}/{draft.sold_photos or 0}. "
        "Можно отправлять по одной или альбомом.",
        reply_markup=(
            inline([[("✅ Выбранные загружены", f"sale:finalize:{draft.id}", "success")]])
            if draft.sold_photos and count >= draft.sold_photos
            else None
        ),
    )


async def selected_count_from_id(draft_id):
    async with Session() as session:
        return await selected_count(session, draft_id)


async def begin_for_booking(message, state, actor, roles, booking_id):
    async with Session() as session:
        booking = await session.get(Booking, booking_id)
        if not await can_sell(actor, roles, booking):
            return await message.answer("Эта запись ещё не готова к продаже или недоступна.")
        draft = await active_draft(session, booking.id)
        if draft is not None and draft.created_by_id != actor.id:
            return await message.answer(
                "Эту продажу уже оформляет другой сотрудник. Завершите тот процесс "
                "или обратитесь к администратору."
            )
        if draft is None:
            draft = SaleDraft(
                booking_id=booking.id,
                created_by_id=actor.id,
                status="AWAITING_RECEIPT",
            )
            session.add(draft)
            await session.flush()
            await audit(
                session,
                actor,
                "sale_draft_started",
                "sale_draft",
                draft.id,
                f"booking={booking.id}",
            )
            await session.commit()
    await state.clear()
    await continue_draft(message, state, draft)


@r.message(F.text == "🧾 Продажа")
async def begin(m, state, current_user, current_roles):
    async with Session() as session:
        rows = await ready_bookings(session, current_user, current_roles)
    await state.clear()
    if not rows:
        return await m.answer(
            "Съёмок со статусом «Готово к продаже» пока нет."
        )
    await state.set_state(S.booking)
    await m.answer(
        "Выберите съёмку для оформления продажи:",
        reply_markup=inline(
            [[
                (
                    f"#{booking.id} · {booking.shoot_date:%d.%m} {booking.shoot_time:%H:%M}",
                    f"sale:start:{booking.id}",
                    "success",
                )
            ] for booking in rows]
        ),
    )


@r.callback_query(F.data.startswith("sale:start:"))
async def start_sale(c, state, current_user, current_roles):
    booking_id = positive_id(c.data.rsplit(":", 1)[1])
    if booking_id is None or c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    await c.answer()
    await begin_for_booking(
        c.message, state, current_user, current_roles, booking_id
    )


@r.message(S.booking, F.text)
async def booking_by_number(m, state, current_user, current_roles):
    booking_id = positive_id((m.text or "").strip())
    if booking_id is None:
        return await m.answer("Выберите запись кнопкой или введите её номер.")
    await begin_for_booking(
        m, state, current_user, current_roles, booking_id
    )


@r.message(S.receipt, F.photo)
async def sale_receipt(m, state, current_user):
    data = await state.get_data()
    draft_id = positive_id(data.get("sale_draft_id"))
    photo = m.photo[-1]
    if not draft_id:
        await state.clear()
        return await m.answer("Начните продажу заново.")
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        return await m.answer("Пришлите фото чека размером до 8 МБ.")
    try:
        content = await download_receipt(m.bot, photo)
        digest = hashlib.sha256(content).hexdigest()
        analysis = await extract_receipt(content)
        op_key = operation_key(analysis.get("fields", {}))
    except (
        TelegramAPIError,
        aiohttp.ClientError,
        OSError,
        TimeoutError,
        ValueError,
    ):
        return await m.answer("Не удалось прочитать чек. Пришлите фотографию ещё раз.")

    async with Session() as session:
        draft = await session.get(SaleDraft, draft_id, with_for_update=True)
        if (
            draft is None
            or draft.created_by_id != current_user.id
            or draft.status != "AWAITING_RECEIPT"
        ):
            await state.clear()
            return await m.answer("Этот черновик продажи уже закрыт.")
        existing = await session.scalar(
            select(Receipt.id).where(
                or_(
                    Receipt.file_unique_id == photo.file_unique_id,
                    Receipt.image_sha256 == digest,
                    Receipt.operation_key == op_key if op_key else False,
                )
            ).limit(1)
        )
        draft_duplicate = await session.scalar(
            select(SaleDraft.id).where(
                SaleDraft.id != draft.id,
                or_(
                    SaleDraft.receipt_file_unique_id == photo.file_unique_id,
                    SaleDraft.receipt_image_sha256 == digest,
                    SaleDraft.receipt_operation_key == op_key if op_key else False,
                ),
            ).limit(1)
        )
        if existing or draft_duplicate:
            return await m.answer(
                "⚠️ Этот чек уже использовался или похож на ранее загруженный. "
                "Пришлите другой чек."
            )
        draft.receipt_file_id = photo.file_id
        draft.receipt_file_unique_id = photo.file_unique_id
        draft.receipt_image_sha256 = digest
        draft.receipt_operation_key = op_key
        draft.receipt_analysis = json.dumps(analysis, ensure_ascii=False)
        draft.status = "AWAITING_COUNTS"
        await audit(
            session,
            current_user,
            "sale_receipt_captured",
            "sale_draft",
            draft.id,
        )
        await session.commit()
    await state.set_state(S.total)
    status = analysis.get("status")
    note = (
        "✅ Чек распознан."
        if status == "extracted"
        else "✅ Чек сохранён. Если распознавание неполное, владелец проверит его вручную."
    )
    await m.answer(
        note
        + "\n\nТеперь введите ОБЩЕЕ количество кадров в этой фотосессии. "
          "Процент фотографа по этому числу не рассчитывается."
    )


@r.message(S.receipt)
async def require_receipt(m):
    await m.answer("Сначала отправьте фотографию чека продажи.")


@r.message(S.total, F.text)
async def sale_total_frames(m, state, current_user):
    try:
        total = int((m.text or "").strip())
        if not 0 < total <= 10000:
            raise ValueError
    except ValueError:
        return await m.answer("Введите количество кадров целым числом от 1 до 10000.")
    data = await state.get_data()
    draft_id = positive_id(data.get("sale_draft_id"))
    async with Session() as session:
        draft = await session.get(SaleDraft, draft_id, with_for_update=True)
        if (
            draft is None
            or draft.created_by_id != current_user.id
            or draft.status != "AWAITING_COUNTS"
        ):
            await state.clear()
            return await m.answer("Черновик продажи уже закрыт.")
        draft.declared_photo_count = total
        await session.commit()
    await state.set_state(S.sold)
    await m.answer(
        f"Всего кадров указано: {total}.\n"
        "Теперь введите количество ПРОДАННЫХ кадров."
    )


@r.message(S.sold, F.text)
async def sale_sold_frames(m, state, current_user):
    try:
        sold = int((m.text or "").strip())
        if not 0 < sold <= 10000:
            raise ValueError
    except ValueError:
        return await m.answer("Введите положительное целое количество проданных кадров.")
    data = await state.get_data()
    draft_id = positive_id(data.get("sale_draft_id"))
    async with Session() as session:
        draft = await session.get(SaleDraft, draft_id, with_for_update=True)
        if (
            draft is None
            or draft.created_by_id != current_user.id
            or draft.status != "AWAITING_COUNTS"
            or not draft.declared_photo_count
        ):
            await state.clear()
            return await m.answer("Черновик продажи уже закрыт.")
        if sold > draft.declared_photo_count:
            return await m.answer(
                "Проданных кадров не может быть больше общего количества кадров."
            )
        booking = await session.get(Booking, draft.booking_id)
        package = await session.get(Package, booking.package_id) if booking else None
        if booking is None or package is None:
            await state.clear()
            return await m.answer("У записи не найден пакет. Обратитесь к администратору.")
        already_sold = int(
            await session.scalar(
                select(func.coalesce(func.sum(Sale.sold_photos), 0)).where(
                    Sale.booking_id == booking.id
                )
            )
            or 0
        )
        if already_sold + sold > draft.declared_photo_count:
            return await m.answer(
                f"Уже продано: {already_sold}. После этой продажи получится "
                f"{already_sold + sold}, что больше указанных {draft.declared_photo_count} кадров."
            )
        try:
            price = Decimal(str(number(package.price_per_photo, "Цена", minimum=0.01)))
        except ValueError:
            await state.clear()
            return await m.answer("Некорректная цена пакета. Обратитесь к администратору.")
        amount = (Decimal(sold) * price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        total_before, paid_before, _ = await payment_totals(session, booking.id)
        expected_payment = max(
            money(total_before) + amount - money(paid_before),
            Decimal("0.00"),
        )
        draft.sold_photos = sold
        draft.expected_amount = expected_payment if expected_payment > 0 else None
        draft.status = "AWAITING_SELECTED"
        await session.commit()
    await state.set_state(S.selected)
    await m.answer(
        f"💰 Продано: {sold} кадров. Сумма продажи: {amount:.2f} ₽.\n\n"
        f"Теперь загрузите ВЫБРАННЫЕ фотографии: 0/{sold}. "
        "Это обязательный минимум для завершения продажи. "
        "Всю съёмку сейчас загружать не нужно."
    )


async def selected_storage():
    token, client_id = configured_from_env()
    if not token:
        raise YandexDiskError("YANDEX_DISK_TOKEN is not configured")
    storage = YandexDisk(token, client_id)
    state = await storage.verify(write_test=False)
    if not state.get("connected"):
        raise YandexDiskError("Yandex.Disk unavailable")
    return storage


@r.message(S.selected, F.photo | F.document)
async def selected_photo(m, state, current_user):
    data = await state.get_data()
    draft_id = positive_id(data.get("sale_draft_id"))
    if not draft_id:
        await state.clear()
        return await m.answer("Начните продажу заново.")
    attachment = m.photo[-1] if m.photo else m.document
    if m.document:
        if attachment.file_size and attachment.file_size > MAX_TELEGRAM_IMAGE_BYTES:
            return await m.answer("Файл больше 20 МБ.")
        if attachment.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            return await m.answer("Выбранные кадры можно отправлять только как JPEG, PNG или WebP.")
    async with Session() as session:
        draft = await session.get(SaleDraft, draft_id)
        if (
            draft is None
            or draft.created_by_id != current_user.id
            or draft.status != "AWAITING_SELECTED"
            or not draft.sold_photos
        ):
            await state.clear()
            return await m.answer("Черновик продажи уже закрыт.")
        current = await selected_count(session, draft.id)
        if current >= draft.sold_photos:
            return await m.answer(
                "Все выбранные фотографии уже загружены. Нажмите «Выбранные загружены»."
            )
        exists = await session.scalar(
            select(SaleDraftPhoto.id).where(
                SaleDraftPhoto.draft_id == draft.id,
                SaleDraftPhoto.telegram_unique_id == attachment.file_unique_id,
            )
        )
        if exists:
            return await m.answer("Этот выбранный кадр уже загружен.")

    buffer = LimitedBuffer()
    try:
        await m.bot.download(attachment.file_id, destination=buffer, timeout=40)
        payload = buffer.getvalue()
        ext, mime = image_format(payload)
        digest = hashlib.sha256(payload).hexdigest()
        storage = await selected_storage()
        base = ROOT + "/sales"
        draft_dir = base + f"/draft-{draft_id}"
        selected_dir = draft_dir + "/selected"
        await storage.ensure_dir(base)
        await storage.ensure_dir(draft_dir)
        await storage.ensure_dir(selected_dir)
        path = selected_dir + f"/{current + 1}_{digest[:12]}.{ext}"
        await storage.upload_bytes(path, payload, content_type=mime)
    except (OSError, ValueError, YandexDiskError):
        return await m.answer(
            "Не удалось сохранить выбранный кадр. Повторите отправку чуть позже."
        )

    async with Session() as session:
        draft = await session.get(SaleDraft, draft_id, with_for_update=True)
        if draft is None or draft.status != "AWAITING_SELECTED":
            return await m.answer("Черновик продажи уже закрыт.")
        try:
            session.add(
                SaleDraftPhoto(
                    draft_id=draft.id,
                    telegram_file_id=attachment.file_id,
                    telegram_unique_id=attachment.file_unique_id,
                    storage_path=path,
                    sha256=digest,
                    byte_size=len(payload),
                )
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
        current = await selected_count(session, draft.id)
        needed = draft.sold_photos
    markup = (
        inline([[("✅ Выбранные загружены", f"sale:finalize:{draft_id}", "success")]])
        if current >= needed
        else None
    )
    await m.answer(
        f"✅ Выбранные фотографии: {current}/{needed}.",
        reply_markup=markup,
    )


@r.message(S.selected)
async def require_selected(m):
    await m.answer(
        "Загрузите выбранные JPEG/PNG/WebP фотографии. "
        "Вся съёмка загружается отдельно и может быть добавлена позже."
    )


@r.callback_query(F.data.startswith("sale:finalize:"))
async def finalize_sale(c, state, current_user, current_roles):
    draft_id = positive_id(c.data.rsplit(":", 1)[1])
    if draft_id is None or c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        draft = await session.get(SaleDraft, draft_id, with_for_update=True)
        if draft is None or draft.status != "AWAITING_SELECTED":
            return await c.answer("Продажа уже завершена или отменена.", show_alert=True)
        booking = await session.get(Booking, draft.booking_id)
        if draft.created_by_id != current_user.id or not await can_sell(
            current_user, current_roles, booking
        ):
            return await c.answer("Нет доступа к этой продаже.", show_alert=True)
        count = await selected_count(session, draft.id)
        if not draft.sold_photos or count < draft.sold_photos:
            return await c.answer(
                f"Сначала загрузите все выбранные фотографии: {count}/{draft.sold_photos or 0}.",
                show_alert=True,
            )
        if not booking.photographer_id:
            return await c.answer("У записи не назначен фотограф.", show_alert=True)
        photographer = await session.get(User, booking.photographer_id)
        manager = await session.get(User, booking.manager_id)
        if photographer is None:
            return await c.answer("Фотограф не найден.", show_alert=True)
        package = await session.get(Package, booking.package_id)
        if package is None:
            return await c.answer("Пакет не найден.", show_alert=True)
        amount = money(draft.expected_amount or 0)
        # expected_amount is the still-unpaid part after approved deposits; the sale itself
        # must always use sold_photos * package price.
        sale_amount = (
            Decimal(draft.sold_photos)
            * Decimal(str(package.price_per_photo))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        percent, actual_full_count = await final_photographer_percent(
            session, booking.id
        )
        if percent is None:
            photographer_percent_value = Decimal(0)
            photographer_commission = Decimal(0)
            finalized_at = None
        else:
            photographer_percent_value = Decimal(str(percent))
            photographer_commission = (
                sale_amount * photographer_percent_value / 100
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            finalized_at = datetime.now(UTC).replace(tzinfo=None)
        sale = Sale(
            booking_id=booking.id,
            created_by_id=current_user.id,
            credited_user_id=photographer.id,
            commission_role="PHOTOGRAPHER",
            sold_photos=draft.sold_photos,
            declared_photo_count=draft.declared_photo_count,
            source_draft_id=draft.id,
            amount=float(sale_amount),
            percent=float(photographer_percent_value),
            commission=float(photographer_commission),
            commission_finalized_at=finalized_at,
        )
        session.add(sale)
        await session.flush()

        receipt = None
        if draft.receipt_file_id and amount > 0:
            receipt = Receipt(
                booking_id=booking.id,
                uploaded_by_id=current_user.id,
                purpose="PAYMENT",
                file_id=draft.receipt_file_id,
                file_unique_id=draft.receipt_file_unique_id,
                image_sha256=draft.receipt_image_sha256,
                operation_key=draft.receipt_operation_key,
                expected_amount=amount,
                analysis=draft.receipt_analysis,
                status="PENDING",
            )
            session.add(receipt)
            await session.flush()

        if manager is not None:
            manager_percent_value = Decimal(
                str(
                    await setting(
                        session,
                        "MANAGER_PERCENT",
                        config.manager_percent,
                    )
                )
            )
            manager_amount = (
                sale_amount * manager_percent_value / 100
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            session.add(
                PayrollEntry(
                    user_id=manager.id,
                    kind="Комиссия менеджера",
                    amount=float(manager_amount),
                    period=datetime.now(UTC).date().isoformat(),
                    note=f"sale={sale.id};booking={booking.id}",
                )
            )

        draft.status = "COMPLETED"
        draft.completed_at = datetime.now(UTC).replace(tzinfo=None)
        await audit(
            session,
            current_user,
            "sale_created",
            "sale",
            sale.id,
            (
                f"booking={booking.id};sold={draft.sold_photos};"
                f"declared={draft.declared_photo_count};selected={count};"
                f"full_uploaded={actual_full_count};"
                f"photographer_percent={percent if percent is not None else 'PENDING'}"
            ),
        )
        await refresh_payment_statuses(session, booking.id)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return await c.answer(
                "Продажа уже была сохранена другим запросом или чек уже использован.",
                show_alert=True,
            )

    await state.clear()
    await c.answer()
    if percent is None:
        rate_text = (
            "📈 Процент фотографа: ожидает полной загрузки съёмки. "
            "Выбранные/проданные кадры на ставку не влияют."
        )
    else:
        rate_text = (
            f"📈 Полная съёмка уже загружена ({actual_full_count} кадров). "
            f"Ставка фотографа: {percent:g}%."
        )
    await c.message.answer(
        f"✅ Продажа #{sale.id} оформлена.\n"
        f"Выбранные фотографии: {count}. Продано: {draft.sold_photos}.\n"
        f"Сумма продажи: {sale_amount:.2f} ₽.\n"
        + (
            f"🧾 Чек #{receipt.id} передан на проверку владельцу.\n"
            if receipt is not None
            else "🧾 Дополнительный платёж по этой продаже не требуется.\n"
        )
        + rate_text
    )
    shooting = None
    async with Session() as session:
        shooting = await session.scalar(
            select(Shooting).where(Shooting.booking_id == booking.id)
        )
    if percent is None and shooting is not None:
        try:
            await c.bot.send_message(
                photographer.tg_id,
                f"☁️ По продаже #{sale.id} выбранные фотографии уже загружены. "
                "Чтобы Photo Boss зафиксировал ваш процент, загрузите всю съёмку.",
                reply_markup=inline(
                    [[
                        (
                            "☁️ Загрузить всю съёмку",
                            f"photo:full_upload:{shooting.id}",
                            "primary",
                        )
                    ]]
                ),
                disable_notification=False,
            )
        except TelegramAPIError:
            pass
