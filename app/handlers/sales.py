import hashlib

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline
from ..models import (
    Booking,
    SaleDraft,
    SaleDraftPhoto,
    Shooting,
)
from ..services.photo_storage import (
    MAX_TELEGRAM_IMAGE_BYTES,
    LimitedBuffer,
    image_format,
)
from ..services.receipts import (
    MAX_IMAGE_BYTES,
    download_receipt,
    extract_receipt,
)
from ..services.sale_workflow import (
    active_draft,  # noqa: F401 - compatibility export
    can_sell,  # noqa: F401 - compatibility export
    capture_draft_receipt,
    complete_sale,
    selected_count,
    set_sale_counts,
    start_sale_draft,
)
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
        try:
            draft = await start_sale_draft(session, actor, roles, booking_id)
        except ValueError as exc:
            return await message.answer(str(exc))
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
        try:
            await capture_draft_receipt(session, current_user, draft, photo.file_id,
                                        photo.file_unique_id, digest, analysis)
        except ValueError as exc:
            return await m.answer(str(exc))
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
        try:
            amount = await set_sale_counts(session, current_user, draft, draft.declared_photo_count, sold)
        except ValueError as exc:
            return await m.answer(str(exc))
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
        try:
            result = await complete_sale(session, current_user, draft_id)
        except ValueError as exc:
            return await c.answer(str(exc), show_alert=True)
        sale, draft, booking = result.sale, result.draft, result.booking
        photographer, receipt = result.photographer, result.receipt
        count, percent = result.count, result.percent
        actual_full_count, sale_amount = result.actual_full_count, result.sale_amount
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
