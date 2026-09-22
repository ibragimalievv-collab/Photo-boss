from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile
from sqlalchemy import select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline
from ..models import (
    BankReconciliation,
    Booking,
    Client,
    Receipt,
    RepeatSaleLead,
    Shooting,
    User,
)
from ..services.bookings import notify_photographer_assignment
from ..services.core import audit, roles_of
from ..services.operations import (
    backup_payload,
    owner_kpis,
    ranked_photographers,
    reconciliation_status,
)

r = Router()
r.message.filter(StaffFilter("MANAGER", "PHOTOGRAPHER"), F.text)
r.callback_query.filter(StaffFilter("MANAGER", "PHOTOGRAPHER"))


class BankFlow(StatesGroup):
    reference = State()
    amount = State()


def positive_id(value):
    try:
        result = int(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


@r.message(F.text == "📈 KPI бизнеса")
async def kpi_dashboard(message, current_roles):
    if "OWNER" not in current_roles:
        return
    end = datetime.now(UTC).date()
    start = end - timedelta(days=29)
    async with Session() as session:
        kpi = await owner_kpis(session, start, end)
    await message.answer(
        "📈 KPI бизнеса · 30 дней\n\n"
        f"Записей: {kpi['bookings']}\n"
        f"Отказов: {kpi['rejected']}\n"
        f"Конверсия без отказа: {kpi['conversion']:.1f}%\n"
        f"Продаж: {kpi['sales_count']} на {kpi['sales_total']:.2f} ₽\n"
        f"Средний чек: {kpi['average_check']:.2f} ₽\n"
        f"Подтверждено поступлений: {kpi['paid']:.2f} ₽"
    )


@r.message(F.text == "🧠 Умное назначение")
async def smart_assignment(message, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        return
    async with Session() as session:
        bookings = (
            await session.scalars(
                select(Booking)
                .where(Booking.photographer_id.is_(None), Booking.status != "REJECTED")
                .order_by(Booking.shoot_date, Booking.shoot_time)
                .limit(20)
            )
        ).all()
    if not bookings:
        return await message.answer("Все актуальные съёмки уже распределены.")
    await message.answer(
        "🧠 Выберите запись — бот предложит фотографов по загрузке, отелю и рейтингу:",
        reply_markup=inline(
            [[(f"#{b.id} · {b.shoot_date:%d.%m} {b.shoot_time:%H:%M}", f"smart:booking:{b.id}")]
             for b in bookings]
        ),
    )


@r.callback_query(F.data.startswith("smart:booking:"))
async def smart_booking(callback, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        return await callback.answer("Недостаточно прав.", show_alert=True)
    booking_id = positive_id(callback.data.rsplit(":", 1)[1])
    async with Session() as session:
        booking = await session.get(Booking, booking_id) if booking_id else None
        ranking = await ranked_photographers(session, booking) if booking else []
    if booking is None:
        return await callback.answer("Запись не найдена.", show_alert=True)
    rows = [
        [(f"{user.name} · загрузка {load} · рейтинг {quality:.1f}", f"smart:assign:{booking.id}:{user.id}")]
        for user, _score, load, quality in ranking[:10]
    ]
    await callback.answer()
    await callback.message.answer(
        f"Рекомендации для записи #{booking.id}:\nПервый кандидат — оптимальный по суммарному баллу.",
        reply_markup=inline(rows) if rows else None,
    )


@r.callback_query(F.data.startswith("smart:assign:"))
async def smart_assign(callback, current_user, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        return await callback.answer("Недостаточно прав.", show_alert=True)
    try:
        _, _, raw_booking, raw_user = callback.data.split(":")
        booking_id, user_id = int(raw_booking), int(raw_user)
    except (TypeError, ValueError):
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        booking = await session.get(Booking, booking_id, with_for_update=True)
        user = await session.get(User, user_id)
        if booking is None or user is None or "PHOTOGRAPHER" not in await roles_of(session, user):
            return await callback.answer("Запись или фотограф недоступны.", show_alert=True)
        booking.photographer_id = user.id
        if booking.status in {"NEW", "CONFIRMED", "PENDING_CONFIRMATION", "RESCHEDULED"}:
            booking.status = "ASSIGNED"
        shooting = await session.scalar(
            select(Shooting).where(Shooting.booking_id == booking.id)
        )
        if shooting and shooting.status in {
            "PENDING_CONFIRMATION", "CONFIRMED", "ASSIGNED"
        }:
            shooting.status = "ASSIGNED"
        await audit(session, current_user, "smart_assignment", "booking", booking.id, f"photographer={user.id}")
        await session.commit()
        await notify_photographer_assignment(callback.bot, session, booking)
    await callback.answer("Фотограф назначен.")
    await callback.message.answer(f"✅ На запись #{booking_id} назначен {user.name}.")


@r.message(F.text == "🔁 Повторные продажи")
async def repeat_sales(message, current_user, current_roles):
    async with Session() as session:
        query = select(RepeatSaleLead, Booking, Client).join(
            Booking, Booking.id == RepeatSaleLead.booking_id
        ).join(Client, Client.id == Booking.client_id).where(RepeatSaleLead.status == "NEW")
        if "MANAGER" in current_roles and not {"OWNER", "ADMIN"} & current_roles:
            query = query.where(RepeatSaleLead.manager_id == current_user.id)
        rows = (await session.execute(query.order_by(RepeatSaleLead.created_at).limit(30))).all()
    if not rows:
        return await message.answer("Новых клиентов для повторной продажи пока нет.")
    for lead, booking, client in rows:
        await message.answer(
            f"🔁 Запись #{booking.id}\nКлиент: {client.name}\nТелефон: {client.phone or 'не указан'}\n\n{lead.offer_text}",
            reply_markup=inline([[("✅ Связались", f"repeat:contacted:{lead.id}"), ("Не интересно", f"repeat:declined:{lead.id}")]]),
        )


@r.callback_query(F.data.startswith("repeat:"))
async def repeat_status(callback, current_user, current_roles):
    try:
        _, status, raw_id = callback.data.split(":")
        lead_id = int(raw_id)
    except (TypeError, ValueError):
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    new_status = {"contacted": "CONTACTED", "declined": "DECLINED"}.get(status)
    if new_status is None:
        return await callback.answer("Некорректный статус.", show_alert=True)
    async with Session() as session:
        lead = await session.get(RepeatSaleLead, lead_id, with_for_update=True)
        if lead is None or ("MANAGER" in current_roles and not {"OWNER", "ADMIN"} & current_roles and lead.manager_id != current_user.id):
            return await callback.answer("Лид недоступен.", show_alert=True)
        lead.status = new_status
        lead.contacted_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
    await callback.answer("Сохранено.")


@r.message(F.text == "💾 Резервная копия")
async def backup(message, current_roles):
    if "OWNER" not in current_roles:
        return
    async with Session() as session:
        payload = await backup_payload(session)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    await message.answer_document(
        BufferedInputFile(payload, filename=f"photo-boss-backup-{stamp}.json"),
        caption="💾 Экспорт сотрудников, гостей, броней и продаж. Это не полная копия базы данных. Токены, пароли и фотографии чеков не включены.",
    )


@r.message(F.text == "🏦 Сверка банка")
async def bank_queue(message, current_roles):
    if "OWNER" not in current_roles:
        return
    async with Session() as session:
        receipts = (
            await session.scalars(
                select(Receipt).where(
                    Receipt.status == "APPROVED",
                    ~Receipt.id.in_(select(BankReconciliation.receipt_id)),
                ).order_by(Receipt.reviewed_at.desc()).limit(30)
            )
        ).all()
    if not receipts:
        return await message.answer("Все подтверждённые чеки уже сверены с банком.")
    await message.answer(
        "🏦 Выберите чек для ручной сверки с банковской выпиской:",
        reply_markup=inline([[(f"Чек #{r.id} · {r.verified_amount:.2f} ₽", f"bank:start:{r.id}")] for r in receipts]),
    )


@r.callback_query(F.data.startswith("bank:start:"))
async def bank_start(callback, state, current_roles):
    if "OWNER" not in current_roles:
        return await callback.answer("Недостаточно прав.", show_alert=True)
    receipt_id = positive_id(callback.data.rsplit(":", 1)[1])
    if receipt_id is None:
        return await callback.answer("Некорректный чек.", show_alert=True)
    await state.set_state(BankFlow.reference)
    await state.update_data(bank_receipt_id=receipt_id)
    await callback.answer()
    await callback.message.answer("Введите уникальный номер операции из банковской выписки:")


@r.message(BankFlow.reference)
async def bank_reference(message, state):
    reference = message.text.strip()
    if not 4 <= len(reference) <= 150:
        return await message.answer("Введите номер операции длиной от 4 до 150 символов.")
    await state.update_data(bank_reference=reference)
    await state.set_state(BankFlow.amount)
    await message.answer("Введите сумму операции из банковской выписки:")


@r.message(BankFlow.amount)
async def bank_amount(message, state, current_user):
    try:
        amount = Decimal(message.text.strip().replace(",", ".")).quantize(Decimal("0.01"))
        if amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return await message.answer("Введите положительную сумму, например 1500.00")
    data = await state.get_data()
    async with Session() as session:
        receipt = await session.get(Receipt, data["bank_receipt_id"])
        if receipt is None or receipt.status != "APPROVED":
            await state.clear()
            return await message.answer("Чек больше недоступен для сверки.")
        match = reconciliation_status(receipt.verified_amount, amount)
        session.add(BankReconciliation(
            receipt_id=receipt.id, provider="MANUAL", bank_reference=data["bank_reference"],
            amount=amount, status=match, matched_by_id=current_user.id,
        ))
        await audit(session, current_user, "bank_reconciled", "receipt", receipt.id, f"status={match}")
        await session.commit()
    await state.clear()
    await message.answer("✅ Сумма совпала." if match == "MATCHED" else "⚠️ Сумма не совпадает. Проверьте операцию вручную.")
