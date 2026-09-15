from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ReplyKeyboardRemove
from sqlalchemy import func, select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline, reply, request_location
from ..models import (
    Booking,
    Client,
    Hotel,
    PayrollEntry,
    Photo,
    Sale,
    ShiftCheckIn,
    Shooting,
)
from ..services.core import ROLES, audit, get_user, has, menu, roles_of
from ..services.shifts import LATE_FINE, is_late, shift_now

r = Router()
r.message.filter(StaffFilter("PHOTOGRAPHER"))
r.callback_query.filter(StaffFilter("PHOTOGRAPHER"))


class ShiftFlow(StatesGroup):
    location = State()
    photo = State()


async def today_check_in(session, user_id, *, lock=False):
    query = select(ShiftCheckIn).where(
        ShiftCheckIn.user_id == user_id,
        ShiftCheckIn.shift_date == shift_now().date(),
    )
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).one_or_none()


async def ask_for_location(message, state):
    await state.set_state(ShiftFlow.location)
    await message.answer(
        "📍 Для начала смены отправьте текущее местоположение кнопкой ниже.",
        reply_markup=request_location(),
    )


async def ask_for_full_body_photo(message, state):
    await state.set_state(ShiftFlow.photo)
    await message.answer(
        "📷 Сфотографируйтесь сейчас в полный рост. Нажмите значок камеры/скрепки "
        "в Telegram, выберите «Камера» и отправьте фотографию сюда.",
        reply_markup=ReplyKeyboardRemove(),
    )


@r.message(F.text == "🔄 Моя смена")
async def my_shift(m, state, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await m.answer("Смена доступна сотруднику с ролью фотографа.")
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        check_in = await today_check_in(s, u.id)
    if check_in is None:
        await state.clear()
        return await m.answer(
            "🔄 Моя смена\n\nНачало рабочего дня — 09:00. "
            "После 09:00 автоматически начисляется штраф 500 ₽.",
            reply_markup=inline([[("▶️ Начать смену", "shift:begin")]]),
        )
    if check_in.status == "STARTED":
        await state.clear()
        started = shift_now(check_in.started_at)
        result = f"✅ Смена начата в {started:%H:%M}. Геолокация и фото сохранены."
        if check_in.late:
            result += f"\n⚠️ Опоздание: штраф {check_in.fine_amount:.0f} ₽."
        return await m.answer(result, reply_markup=reply(menu(current_roles)))
    if check_in.status == "AWAITING_PHOTO":
        return await ask_for_full_body_photo(m, state)
    return await ask_for_location(m, state)


@r.callback_query(F.data == "shift:begin")
async def begin_shift(c: CallbackQuery, state, current_roles):
    if c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    if "PHOTOGRAPHER" not in current_roles:
        return await c.answer("Нужна роль фотографа.", show_alert=True)
    async with Session() as s:
        u = await get_user(s, c.from_user.id)
        check_in = await today_check_in(s, u.id, lock=True)
        if check_in is None:
            check_in = ShiftCheckIn(
                user_id=u.id,
                shift_date=shift_now().date(),
            )
            s.add(check_in)
            await s.flush()
            await audit(s, u, "shift_check_in_started", "shift_check_in", check_in.id)
            await s.commit()
    await c.answer()
    if check_in.status == "STARTED":
        await state.clear()
        return await c.message.answer("✅ Сегодняшняя смена уже начата.")
    if check_in.status == "AWAITING_PHOTO":
        return await ask_for_full_body_photo(c.message, state)
    await ask_for_location(c.message, state)


@r.message(ShiftFlow.location, F.location)
async def save_shift_location(m, state):
    now = datetime.now(UTC).replace(tzinfo=None)
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        check_in = await today_check_in(s, u.id, lock=True)
        if check_in is None:
            await state.clear()
            return await m.answer("Начните заново через «🔄 Моя смена».")
        if check_in.status == "STARTED":
            await state.clear()
            return await m.answer("✅ Сегодняшняя смена уже начата.")
        check_in.latitude = m.location.latitude
        check_in.longitude = m.location.longitude
        check_in.location_received_at = now
        check_in.status = "AWAITING_PHOTO"
        await audit(s, u, "shift_location_received", "shift_check_in", check_in.id)
        await s.commit()
    await m.answer("✅ Геолокация сохранена.", reply_markup=ReplyKeyboardRemove())
    await ask_for_full_body_photo(m, state)


@r.message(ShiftFlow.location)
async def require_shift_location(m):
    await m.answer(
        "Нужно отправить геолокацию именно кнопкой «📍 Поделиться местоположением».",
        reply_markup=request_location(),
    )


@r.message(ShiftFlow.photo, F.photo)
async def save_shift_photo(m, state, current_roles):
    now_local = shift_now()
    now_utc = now_local.astimezone(UTC).replace(tzinfo=None)
    late = is_late(now_local)
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        check_in = await today_check_in(s, u.id, lock=True)
        if check_in is None:
            await state.clear()
            return await m.answer("Начните заново через «🔄 Моя смена».")
        if check_in.status == "STARTED":
            await state.clear()
            return await m.answer("✅ Сегодняшняя смена уже начата.")
        if check_in.status != "AWAITING_PHOTO" or check_in.latitude is None:
            return await ask_for_location(m, state)
        check_in.full_body_file_id = m.photo[-1].file_id
        check_in.started_at = now_utc
        check_in.status = "STARTED"
        check_in.late = late
        check_in.fine_amount = LATE_FINE if late else 0
        if late:
            s.add(
                PayrollEntry(
                    user_id=u.id,
                    kind="Штраф за опоздание",
                    amount=-LATE_FINE,
                    period=check_in.shift_date.isoformat(),
                    note=f"Начало смены после 09:00; shift_check_in={check_in.id}",
                )
            )
        await audit(
            s,
            u,
            "shift_started",
            "shift_check_in",
            check_in.id,
            f"late={late};fine={check_in.fine_amount:.0f}",
        )
        await s.commit()
    await state.clear()
    text = f"✅ Смена начата в {now_local:%H:%M}. Геолокация и фото сохранены."
    if late:
        text += f"\n⚠️ Опоздание после 09:00: начислен штраф {LATE_FINE:.0f} ₽."
    else:
        text += "\nВы отметились вовремя."
    await m.answer(text, reply_markup=reply(menu(current_roles)))


@r.message(ShiftFlow.photo)
async def require_shift_photo(m):
    await m.answer(
        "Нужна фотография в полный рост. Откройте камеру через значок камеры/скрепки "
        "и отправьте снимок сюда.",
        reply_markup=ReplyKeyboardRemove(),
    )


@r.message(F.text == "📸 Мои съёмки")
async def shoots(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rs = await roles_of(s, u)
        if not has(rs, "PHOTOGRAPHER"):
            return
        q = await s.execute(
            select(Booking, Shooting, Hotel, Client)
            .join(Shooting, Shooting.booking_id == Booking.id)
            .join(Hotel, Hotel.id == Booking.hotel_id)
            .join(Client, Client.id == Booking.client_id)
            .where(Booking.photographer_id == u.id)
            .order_by(Booking.shoot_date, Booking.shoot_time)
        )
        rows = q.all()
        if not rows:
            return await m.answer("Съёмок нет.")
        for b, sh, h, c in rows:
            await m.answer(
                f"📸 Съёмка #{b.id}\n🏨 {h.name}\n🚪 {b.room}\n👤 {c.name}\n📅 {b.shoot_date} {b.shoot_time}\nСтатус: {sh.status}",
                reply_markup=inline(
                    [
                        [
                            ("▶️ Принять", f"accept:{sh.id}"),
                            ("📍 Прибыл", f"arrive:{sh.id}"),
                        ],
                        [
                            ("▶️ Начать", f"start:{sh.id}"),
                            ("✅ Готово", f"done:{sh.id}"),
                        ],
                    ]
                ),
            )


@r.callback_query(F.data.startswith(("accept:", "arrive:", "start:", "done:")))
async def action(c: CallbackQuery):
    try:
        act, raw_id = c.data.split(":", 1)
        sid = int(raw_id)
        if not 0 < sid <= 2**31 - 1:
            raise ValueError
    except (TypeError, ValueError):
        return await c.answer("Некорректная кнопка.")
    transitions = {
        "accept": ("ASSIGNED", "ACCEPTED", "accepted_at"),
        "arrive": ("ACCEPTED", "ARRIVED", "arrived_at"),
        "start": ("ARRIVED", "SHOOTING", "started_at"),
        "done": ("SHOOTING", "READY_FOR_MANAGER", "completed_at"),
    }
    async with Session() as s:
        u = await get_user(s, c.from_user.id)
        sh = (
            await s.execute(
                select(Shooting).where(Shooting.id == sid).with_for_update()
            )
        ).scalar_one_or_none()
        if sh is None:
            return await c.answer("Съёмка не найдена.")
        b = await s.get(Booking, sh.booking_id)
        if u is None or not u.active or b is None or b.photographer_id != u.id:
            return await c.answer("Это не ваша съёмка.")
        expected, target, timestamp = transitions[act]
        if sh.status != expected:
            return await c.answer(
                "Этот шаг уже выполнен или предыдущий ещё не завершён."
            )
        sh.status = target
        setattr(sh, timestamp, datetime.now(UTC).replace(tzinfo=None))
        if act == "done":
            b.status = target
        await audit(s, u, f"shooting_{act}", "shooting", sid)
        await s.commit()
        await c.message.answer("Готово: " + target)
        await c.answer()


@r.message(F.text == "📊 Моя статистика")
async def stats(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        n = (
            await s.execute(
                select(func.count(Photo.id))
                .join(Shooting, Shooting.id == Photo.shooting_id)
                .join(Booking, Booking.id == Shooting.booking_id)
                .where(Booking.photographer_id == u.id)
            )
        ).scalar() or 0
        rev = (
            await s.execute(
                select(func.coalesce(func.sum(Sale.amount), 0)).where(
                    Sale.credited_user_id == u.id
                )
            )
        ).scalar() or 0
        await m.answer(f"📊 Статистика\nФото: {n}\nПродажи засчитаны: {rev:.2f} ₽")


@r.message(F.text == "💰 Мои выплаты")
async def payouts(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rows = (
            (
                await s.execute(
                    select(PayrollEntry)
                    .where(PayrollEntry.user_id == u.id)
                    .order_by(PayrollEntry.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        await m.answer(
            "\n".join(f"{x.period}: {x.kind} {x.amount:.2f} ₽" for x in rows)
            or "Выплат пока нет."
        )


@r.message(F.text == "👤 Профиль")
async def profile(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rs = await roles_of(s, u)
        await m.answer(
            f"👤 {u.name}\nID: {u.tg_id}\nРоли: {', '.join(ROLES[x] for x in rs)}"
        )
