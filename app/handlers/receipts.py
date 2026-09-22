import hashlib
import json
import logging
from decimal import InvalidOperation

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import PhotoSize
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline
from ..models import Booking, Receipt, Sale, utc_now
from ..services.core import audit
from ..services.receipts import (
    MAX_IMAGE_BYTES,
    STATUS_NAMES,
    analysis_text,
    download_receipt,
    duplicate_receipt,
    expected_amount,
    extract_receipt,
    money,
    operation_key,
    payment_totals,
    refresh_payment_statuses,
)

logger = logging.getLogger(__name__)
r = Router()
r.message.filter(StaffFilter("MANAGER", "PHOTOGRAPHER"))
r.callback_query.filter(StaffFilter("MANAGER", "PHOTOGRAPHER"))


class ReceiptFlow(StatesGroup):
    booking = State()
    photo = State()
    verified_amount = State()
    confirm = State()


def positive_id(value):
    try:
        result = int(value)
        return result if 0 < result < 2**31 else None
    except (ValueError, TypeError):
        return None


async def can_access(session, booking, user, roles):
    if booking is None:
        return False
    if roles & {"OWNER", "ADMIN"} or user.id in {
        booking.manager_id, booking.photographer_id
    }:
        return True
    return bool(await session.scalar(select(Sale.id).where(
        Sale.booking_id == booking.id, Sale.created_by_id == user.id
    ).limit(1)))


async def request_photo(message, state, booking_id, purpose):
    await state.clear()
    await state.set_state(ReceiptFlow.photo)
    await state.set_data({"receipt_booking": booking_id, "receipt_purpose": purpose})
    label = "брони" if purpose == "DEPOSIT" else "оплаты съёмки"
    await message.answer(
        f"Пришлите одно фото чека {label} для записи #{booking_id}. "
        "Должны читаться сумма, дата, получатель и номер операции.\n"
        "Оплата останется неподтверждённой до проверки владельцем. "
        "При подключённом распознавании фото обрабатывается OpenAI.\n"
        "Отмена: /cancel. Вернуться можно через «🧾 Чеки и оплата»."
    )


@r.message(F.text == "🧾 Чеки и оплата")
async def receipts_menu(m, state):
    await state.clear()
    await state.set_state(ReceiptFlow.booking)
    await m.answer("Введите номер записи для загрузки чека или просмотра оплаты.\nОтмена: /cancel")


@r.message(ReceiptFlow.booking, F.text)
async def find_booking(m, state, current_user, current_roles):
    booking_id = positive_id(m.text)
    async with Session() as session:
        booking = await session.get(Booking, booking_id) if booking_id else None
        if not await can_access(session, booking, current_user, current_roles):
            return await m.answer("Запись не найдена или недоступна.")
        total, paid, outstanding = await payment_totals(session, booking.id)
        receipts = (await session.scalars(select(Receipt).where(
            Receipt.booking_id == booking.id
        ).order_by(Receipt.id.desc()).limit(10))).all()
    await state.clear()
    lines = [f"Запись #{booking.id}: продаж на {total:.2f} ₽.",
             f"Подтверждено владельцем (с учётом брони): {paid:.2f} ₽.",
             f"Осталось оплатить: {outstanding:.2f} ₽."]
    for receipt in receipts:
        label = "Бронь" if receipt.purpose == "DEPOSIT" else "Оплата"
        lines.append(f"{label}, чек #{receipt.id}: {STATUS_NAMES[receipt.status]}.")
    buttons = []
    if booking.deposit > 0:
        buttons.append([("📷 Чек брони", f"receipt:upload:DEPOSIT:{booking.id}")])
    if outstanding > 0:
        buttons.append([("📷 Чек оплаты съёмки", f"receipt:upload:PAYMENT:{booking.id}")])
    await m.answer("\n".join(lines), reply_markup=inline(buttons) if buttons else None)




@r.callback_query(F.data.startswith("receipt:upload:"))
async def start_upload(c, state, current_user, current_roles):
    parts = c.data.split(":")
    if len(parts) != 4 or parts[2] not in {"DEPOSIT", "PAYMENT"}:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    booking_id = positive_id(parts[3])
    async with Session() as session:
        booking = await session.get(Booking, booking_id) if booking_id else None
        if not await can_access(session, booking, current_user, current_roles):
            return await c.answer("Нет доступа к записи.", show_alert=True)
        if await expected_amount(session, booking, parts[2]) <= 0:
            return await c.answer("Дополнительная оплата не требуется.", show_alert=True)
    await c.answer()
    await request_photo(c.message, state, booking.id, parts[2])


@r.message(ReceiptFlow.photo, F.photo)
async def save_photo(m, state, current_user, current_roles):
    data = await state.get_data()
    booking_id = positive_id(data.get("receipt_booking"))
    purpose = data.get("receipt_purpose")
    if not booking_id or purpose not in {"DEPOSIT", "PAYMENT"}:
        await state.clear()
        return await m.answer("Начните загрузку через «🧾 Чеки и оплата».")
    photo = m.photo[-1]
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        return await m.answer("Пришлите фото размером до 8 МБ.")
    async with Session() as session:
        booking = await session.scalar(select(Booking).where(
            Booking.id == booking_id
        ).with_for_update())
        if not await can_access(session, booking, current_user, current_roles):
            await state.clear()
            return await m.answer("Нет доступа к записи.")
        pending = await session.scalar(select(Receipt.id).where(
            Receipt.booking_id == booking_id, Receipt.purpose == purpose,
            Receipt.status == "PENDING"
        ).limit(1))
        if pending:
            await state.clear()
            return await m.answer(f"Чек #{pending} уже ожидает проверки владельца.")
        expected = await expected_amount(session, booking, purpose)
        if expected <= 0:
            await state.clear()
            return await m.answer("Дополнительная оплата не требуется.")
        receipt = Receipt(
            booking_id=booking_id, uploaded_by_id=current_user.id, purpose=purpose,
            file_id=photo.file_id, file_unique_id=photo.file_unique_id,
            expected_amount=expected,
        )
        session.add(receipt)
        try:
            await session.flush()
            await audit(session, current_user, "receipt_uploaded", "receipt", receipt.id)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return await m.answer("Это фото чека уже загружали. Повторно учесть его нельзя.")
    await state.clear()
    await m.answer(f"Чек #{receipt.id} сохранён. Проверяю изображение…")
    receipt, duplicate = await analyze_saved(m.bot, receipt.id, photo)
    notice = "\n⚠️ Найден похожий ранее загруженный чек; нужна проверка." if duplicate else ""
    await m.answer(
        f"Чек #{receipt.id}: {STATUS_NAMES[receipt.status]}.{notice}\n"
        + analysis_text(receipt), parse_mode=None,
    )


async def analyze_saved(bot, receipt_id, photo):
    digest = None
    try:
        content = await download_receipt(bot, photo)
        digest = hashlib.sha256(content).hexdigest()
        analysis = await extract_receipt(content)
    except (TelegramAPIError, aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
        # Attachment is already durable even if downloading/analysis fails or times out.
        logger.warning("Receipt %s analysis failed (%s)", receipt_id, type(exc).__name__)
        analysis = {"status": "download_failed"}
    async with Session() as session:
        receipt = await session.scalar(select(Receipt).where(
            Receipt.id == receipt_id
        ).with_for_update())
        if (receipt.status == "PENDING"
                and json.loads(receipt.analysis or "{}").get("status") != "extracted"):
            receipt.image_sha256 = digest or receipt.image_sha256
            receipt.analysis = json.dumps(analysis, ensure_ascii=False)
            receipt.operation_key = operation_key(analysis.get("fields", {}))
        duplicate = await duplicate_receipt(session, receipt)
        await session.commit()
    return receipt, duplicate


@r.message(ReceiptFlow.photo)
async def photo_required(m):
    await m.answer("Нужно отправить фото чека как фотографию. Для отмены: /cancel.")


async def show_queue(message, offset=0):
    async with Session() as session:
        receipts = (await session.scalars(select(Receipt).where(
            Receipt.status == "PENDING", Receipt.id > offset
        ).order_by(Receipt.id).limit(11))).all()
    if not receipts:
        return await message.answer("Чеков на проверку нет.")
    rows = []
    for receipt in receipts[:10]:
        label = "Бронь" if receipt.purpose == "DEPOSIT" else "Оплата"
        rows.append([(f"#{receipt.id} · {label} · запись #{receipt.booking_id}",
                      f"receipt:review:{receipt.id}")])
    if len(receipts) > 10:
        rows.append([("Далее →", f"receipt:queue:{receipts[9].id}")])
    await message.answer("Чеки ожидают сверки с банковской выпиской:", reply_markup=inline(rows))


@r.message(F.text == "🔎 Проверить чеки")
async def review_menu(m, state, current_roles):
    if "OWNER" not in current_roles:
        return await m.answer("Проверка поступлений доступна только владельцу.")
    await state.clear()
    await show_queue(m)


@r.callback_query(F.data.startswith("receipt:queue:"))
async def queue_page(c, state, current_roles):
    if "OWNER" not in current_roles:
        return await c.answer("Только для владельца.", show_alert=True)
    offset = positive_id(c.data.rsplit(":", 1)[-1]) or 0
    await state.clear()
    await c.answer()
    await show_queue(c.message, offset)


@r.callback_query(F.data.startswith("receipt:review:"))
async def review(c, state, current_roles):
    if "OWNER" not in current_roles:
        return await c.answer("Только для владельца.", show_alert=True)
    receipt_id = positive_id(c.data.rsplit(":", 1)[-1])
    async with Session() as session:
        receipt = await session.get(Receipt, receipt_id) if receipt_id else None
        if receipt is None:
            return await c.answer("Чек не найден.", show_alert=True)
        duplicate = await duplicate_receipt(session, receipt)
    await state.clear()
    await c.answer()
    await c.message.answer_photo(
        receipt.file_id,
        caption=f"Чек #{receipt.id} · запись #{receipt.booking_id}\n"
                f"Ожидалось: {receipt.expected_amount:.2f} ₽.\n"
                f"Статус: {STATUS_NAMES[receipt.status]}.",
    )
    rows = []
    if receipt.status == "PENDING":
        rows = [[("💰 Сверить поступление", f"receipt:amount:{receipt.id}")],
                [("❌ Отклонить чек", f"receipt:reject:{receipt.id}")]]
        if json.loads(receipt.analysis or "{}").get("status") != "extracted":
            rows.append([("🔄 Повторить распознавание", f"receipt:analyze:{receipt.id}")])
    rows.append([("К очереди чеков", "receipt:queue:0")])
    notice = "⚠️ Обнаружен повтор изображения или номера операции.\n" if duplicate else ""
    await c.message.answer(notice + analysis_text(receipt), parse_mode=None,
                           reply_markup=inline(rows))


@r.callback_query(F.data.startswith("receipt:analyze:"))
async def retry_analysis(c, current_roles):
    if "OWNER" not in current_roles:
        return await c.answer("Только для владельца.", show_alert=True)
    receipt_id = positive_id(c.data.rsplit(":", 1)[-1])
    async with Session() as session:
        receipt = await session.get(Receipt, receipt_id) if receipt_id else None
        if receipt is None or receipt.status != "PENDING":
            return await c.answer("Чек уже обработан или не найден.", show_alert=True)
        if json.loads(receipt.analysis or "{}").get("status") == "extracted":
            return await c.answer("Чек уже распознан.", show_alert=True)
    await c.answer()
    await c.message.answer("Повторяю распознавание сохранённого чека…")
    photo = PhotoSize(file_id=receipt.file_id, file_unique_id=receipt.file_unique_id,
                      width=1, height=1)
    receipt, duplicate = await analyze_saved(c.bot, receipt.id, photo)
    notice = "⚠️ Найден повтор чека.\n" if duplicate else ""
    await c.message.answer(notice + analysis_text(receipt), parse_mode=None,
                           reply_markup=inline([[("Открыть чек", f"receipt:review:{receipt.id}")]]))


@r.callback_query(F.data.startswith("receipt:amount:"))
async def ask_verified_amount(c, state, current_roles):
    if "OWNER" not in current_roles:
        return await c.answer("Только для владельца.", show_alert=True)
    receipt_id = positive_id(c.data.rsplit(":", 1)[-1])
    async with Session() as session:
        receipt = await session.get(Receipt, receipt_id) if receipt_id else None
        if receipt is None or receipt.status != "PENDING":
            return await c.answer("Чек уже обработан или не найден.", show_alert=True)
    await state.clear()
    await state.set_state(ReceiptFlow.verified_amount)
    await state.set_data({"review_receipt": receipt.id})
    await c.answer()
    await c.message.answer(
        f"Чек #{receipt.id}. Ожидалось {receipt.expected_amount:.2f} ₽.\n"
        "Откройте банковскую выписку и найдите эту операцию. "
        "Введите фактически поступившую сумму в рублях (без знака валюты).\n"
        "Если поступления нет — /cancel, затем отклоните чек."
    )


@r.message(ReceiptFlow.verified_amount, F.text)
async def verified_amount(m, state, current_roles):
    if "OWNER" not in current_roles:
        await state.clear()
        return await m.answer("Только для владельца.")
    try:
        amount = money(m.text.strip().replace(",", "."))
        if not 0 < amount <= 10_000_000:
            raise ValueError
    except (ValueError, InvalidOperation):
        return await m.answer("Введите сумму от 0,01 до 10 000 000 рублей.")
    data = await state.get_data()
    await state.update_data(verified_amount=str(amount))
    await state.set_state(ReceiptFlow.confirm)
    await m.answer(
        f"Подтвердить поступление {amount:.2f} ₽ по чеку #{data['review_receipt']}?\n"
        "Нажимайте только после сверки операции, получателя и суммы в банковской выписке.",
        reply_markup=inline([[("✅ Деньги поступили", f"receipt:approve:{data['review_receipt']}")]]),
    )


@r.callback_query(F.data.startswith(("receipt:approve:", "receipt:reject:")))
async def decide(c, state, current_user, current_roles):
    if "OWNER" not in current_roles:
        return await c.answer("Только для владельца.", show_alert=True)
    action = c.data.split(":")[1]
    receipt_id = positive_id(c.data.rsplit(":", 1)[-1])
    data = await state.get_data()
    if action == "approve" and (
        await state.get_state() != ReceiptFlow.confirm.state
        or data.get("review_receipt") != receipt_id
    ):
        return await c.answer("Сначала укажите сумму из банковской выписки.", show_alert=True)
    async with Session() as session:
        receipt = await session.scalar(select(Receipt).where(
            Receipt.id == receipt_id
        ).with_for_update()) if receipt_id else None
        if receipt is None or receipt.status != "PENDING":
            return await c.answer("Чек уже обработан или не найден.", show_alert=True)
        # Serialize balance recalculation with other receipts/sales of this booking.
        await session.scalar(select(Booking).where(
            Booking.id == receipt.booking_id
        ).with_for_update())
        if action == "approve":
            if receipt.analysis is None:
                return await c.answer("Дождитесь анализа или нажмите «Повторить распознавание».", show_alert=True)
            if await duplicate_receipt(session, receipt, approved_only=True):
                return await c.answer("Этот платёж уже учтён. Повторное подтверждение запрещено.", show_alert=True)
            receipt.verified_amount = money(data["verified_amount"])
            receipt.approved_image_hash = receipt.image_sha256
            receipt.approved_operation_key = receipt.operation_key
            receipt.status = "APPROVED"
        else:
            receipt.status = "REJECTED"
        receipt.reviewed_by_id = current_user.id
        receipt.reviewed_at = utc_now()
        try:
            await session.flush()
            await refresh_payment_statuses(session, receipt.booking_id)
            await audit(session, current_user, "receipt_" + receipt.status.lower(),
                        "receipt", receipt.id, f"amount={receipt.verified_amount}")
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return await c.answer("Этот платёж уже подтверждён ранее.", show_alert=True)
    await state.clear()
    await c.answer()
    await c.message.answer(f"✅ Чек #{receipt.id}: {STATUS_NAMES[receipt.status]}.",
                           reply_markup=inline([[("Следующие чеки", "receipt:queue:0")]]))
