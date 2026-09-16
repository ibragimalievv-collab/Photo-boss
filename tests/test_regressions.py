from datetime import date, time

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline, reply
from ..models import Booking, Client, Hotel, Package, Sale, Shooting, User, UserRole
from ..services.bookings import booking_card
from ..services.core import audit, get_user, menu

r = Router()
r.message.filter(StaffFilter("MANAGER"), F.text)
r.callback_query.filter(StaffFilter("MANAGER"))


class BookingFlow(StatesGroup):
    hotel = State()
    client_name = State()
    client_phone = State()
    guest_count = State()
    room = State()
    deposit = State()
    shoot_date = State()
    shoot_time = State()
    package = State()
    photographer = State()
    reschedule_date = State()
    reschedule_time = State()


@r.message(F.text == "➕ Новая запись")
async def new_booking(m, state):
    await state.clear()
    async with Session() as s:
        hotels = (
            await s.scalars(select(Hotel).where(Hotel.active.is_(True)).order_by(Hotel.name))
        ).all()
    if not hotels:
        return await m.answer("Нет активных отелей. Сначала добавьте отель.")
    await state.set_state(BookingFlow.hotel)
    await m.answer(
        "➕ Новая запись\n\nВыберите отель:",
        reply_markup=inline([[(hotel.name, f"booking:hotel:{hotel.id}")] for hotel in hotels]),
    )


@r.callback_query(BookingFlow.hotel, F.data.startswith("booking:hotel:"))
async def booking_hotel(c: CallbackQuery, state):
    try:
        hotel_id = int(c.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await c.answer("Некорректный отель.", show_alert=True)
    async with Session() as s:
        hotel = await s.get(Hotel, hotel_id)
    if hotel is None or not hotel.active:
        return await c.answer("Отель недоступен.", show_alert=True)
    await state.update_data(hotel_id=hotel_id)
    await state.set_state(BookingFlow.client_name)
    await c.answer()
    await c.message.answer("Введите имя клиента:")


@r.message(BookingFlow.client_name)
async def booking_client_name(m, state):
    name = m.text.strip()
    if not 2 <= len(name) <= 200:
        return await m.answer("Введите имя клиента от 2 до 200 символов.")
    await state.update_data(client_name=name)
    await state.set_state(BookingFlow.client_phone)
    await m.answer("Введите телефон клиента или отправьте «-», если телефона нет:")


@r.message(BookingFlow.client_phone)
async def booking_client_phone(m, state):
    phone = m.text.strip()
    if phone != "-" and not 5 <= len(phone) <= 80:
        return await m.answer("Введите телефон от 5 до 80 символов либо «-».")
    await state.update_data(client_phone=None if phone == "-" else phone)
    await state.set_state(BookingFlow.guest_count)
    await m.answer("Введите количество гостей на съёмке:")


@r.message(BookingFlow.guest_count)
async def booking_guest_count(m, state):
    try:
        guest_count = int(m.text.strip())
        if not 1 <= guest_count <= 100:
            raise ValueError
    except ValueError:
        return await m.answer("Введите количество гостей числом от 1 до 100.")
    await state.update_data(guest_count=guest_count)
    await state.set_state(BookingFlow.room)
    await m.answer("Введите номер комнаты:")


@r.message(BookingFlow.room)
async def booking_room(m, state):
    room = m.text.strip()
    if not room or len(room) > 100:
        return await m.answer("Введите корректный номер комнаты.")
    await state.update_data(room=room)
    await state.set_state(BookingFlow.deposit)
    await m.answer("Введите сумму брони в рублях или 0:")


@r.message(BookingFlow.deposit)
async def booking_deposit(m, state):
    try:
        deposit = float(m.text.strip().replace(",", "."))
        if not 0 <= deposit <= 10_000_000:
            raise ValueError
    except ValueError:
        return await m.answer("Введите корректную сумму брони, например 1000 или 0.")
    await state.update_data(deposit=deposit)
    await state.set_state(BookingFlow.shoot_date)
    await m.answer("Введите дату съёмки в формате ДД.ММ.ГГГГ:")


@r.message(BookingFlow.shoot_date)
async def booking_date(m, state):
    try:
        day, month, year = map(int, m.text.strip().split("."))
        value = date(year, month, day)
    except (TypeError, ValueError):
        return await m.answer("Неверная дата. Пример: 20.09.2026")
    await state.update_data(shoot_date=value.isoformat())
    await state.set_state(BookingFlow.shoot_time)
    await m.answer("Введите время съёмки в формате ЧЧ:ММ:")


@r.message(BookingFlow.shoot_time)
async def booking_time(m, state):
    try:
        value = time.fromisoformat(m.text.strip())
    except ValueError:
        return await m.answer("Неверное время. Пример: 14:30")
    await state.update_data(shoot_time=value.isoformat())
    async with Session() as s:
        packages = (
            await s.scalars(select(Package).where(Package.active.is_(True)).order_by(Package.name))
        ).all()
    if not packages:
        await state.clear()
        return await m.answer("Нет активных пакетов. Сначала добавьте пакет.")
    await state.set_state(BookingFlow.package)
    await m.answer(
        "Выберите пакет:",
        reply_markup=inline(
            [[(f"{package.name} — {package.price_per_photo:.0f} ₽/фото", f"booking:package:{package.id}")]
             for package in packages]
        ),
    )


@r.callback_query(BookingFlow.package, F.data.startswith("booking:package:"))
async def booking_package(c: CallbackQuery, state):
    try:
        package_id = int(c.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await c.answer("Некорректный пакет.", show_alert=True)
    async with Session() as s:
        package = await s.get(Package, package_id)
        photographers = (
            await s.execute(
                select(User)
                .join(UserRole, UserRole.user_id == User.id)
                .where(User.active.is_(True), UserRole.role == "PHOTOGRAPHER")
                .order_by(User.name)
            )
        ).scalars().all()
    if package is None or not package.active:
        return await c.answer("Пакет недоступен.", show_alert=True)
    await state.update_data(package_id=package_id)
    await state.set_state(BookingFlow.photographer)
    rows = [[("Назначить позже", "booking:photographer:0")]]
    rows += [[(user.name, f"booking:photographer:{user.id}")] for user in photographers]
    await c.answer()
    await c.message.answer("Выберите фотографа:", reply_markup=inline(rows))


@r.callback_query(BookingFlow.photographer, F.data.startswith("booking:photographer:"))
async def booking_photographer(c: CallbackQuery, state, current_roles):
    try:
        photographer_id = int(c.data.rsplit(":", 1)[1]) or None
    except (TypeError, ValueError):
        return await c.answer("Некорректный фотограф.", show_alert=True)
    data = await state.get_data()
    async with Session() as s:
        manager = await get_user(s, c.from_user.id)
        if photographer_id is not None:
            photographer = await s.get(User, photographer_id)
            roles = set(await s.scalars(select(UserRole.role).where(UserRole.user_id == photographer_id)))
            if photographer is None or not photographer.active or "PHOTOGRAPHER" not in roles:
                return await c.answer("Фотограф недоступен.", show_alert=True)
        client = Client(name=data["client_name"], phone=data["client_phone"])
        s.add(client)
        await s.flush()
        booking = Booking(
            hotel_id=data["hotel_id"], client_id=client.id, room=data["room"],
            guest_count=data["guest_count"], deposit=data["deposit"],
            shoot_date=date.fromisoformat(data["shoot_date"]),
            shoot_time=time.fromisoformat(data["shoot_time"]),
            package_id=data["package_id"], manager_id=manager.id,
            photographer_id=photographer_id,
            status="PENDING_CONFIRMATION",
        )
        s.add(booking)
        await s.flush()
        s.add(Shooting(booking_id=booking.id, status="PENDING_CONFIRMATION"))
        await audit(s, manager, "booking_created", "booking", booking.id)
        await s.commit()
    await state.clear()
    await c.answer()
    await c.message.answer(
        f"✅ Запись #{booking.id} создана на {booking.shoot_date:%d.%m.%Y} в {booking.shoot_time:%H:%M}.",
        reply_markup=reply(menu(current_roles)),
    )


@r.message(F.text == "📋 Мои записи")
async def bookings(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rows = (
            await s.scalars(
                select(Booking)
                .where(Booking.manager_id == u.id)
                .order_by(Booking.shoot_date.desc(), Booking.shoot_time.desc())
                .limit(50)
            )
        ).all()
        if not rows:
            return await m.answer("Записей нет.")
        for booking in rows:
            await m.answer(
                await booking_card(s, booking),
                reply_markup=inline(
                    [
                        [
                            ("✅ Подтверждена", f"booking:confirm:{booking.id}", "success"),
                            ("❌ Отказана", f"booking:reject:{booking.id}", "danger"),
                        ],
                        [("📅 Перенесена", f"booking:reschedule:{booking.id}", "primary")],
                        [("🔔 Напомнить гостю", f"booking:remind:{booking.id}", "primary")],
                    ]
                ),
            )


async def owned_booking(session, telegram_id, booking_id, *, lock=False):
    manager = await get_user(session, telegram_id)
    query = select(Booking).where(
        Booking.id == booking_id, Booking.manager_id == manager.id
    )
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).one_or_none(), manager


def callback_booking_id(data):
    try:
        value = int(data.rsplit(":", 1)[1])
        return value if 0 < value <= 2**31 - 1 else None
    except (AttributeError, TypeError, ValueError):
        return None


@r.callback_query(F.data.startswith(("booking:confirm:", "booking:reject:")))
async def set_booking_decision(c: CallbackQuery):
    booking_id = callback_booking_id(c.data)
    if booking_id is None or c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    action = c.data.split(":", 2)[1]
    async with Session() as s:
        booking, manager = await owned_booking(s, c.from_user.id, booking_id, lock=True)
        if booking is None:
            return await c.answer("Запись не найдена.", show_alert=True)
        shooting = (
            await s.scalars(select(Shooting).where(Shooting.booking_id == booking.id))
        ).one_or_none()
        if action == "confirm":
            booking.status = "CONFIRMED"
            if shooting:
                shooting.status = "ASSIGNED"
            audit_action = "booking_confirmed"
            answer = "✅ Съёмка подтверждена."
        else:
            booking.status = "REJECTED"
            if shooting:
                shooting.status = "REJECTED"
            audit_action = "booking_rejected"
            answer = "❌ Съёмка отказана."
        await audit(s, manager, audit_action, "booking", booking.id)
        await s.commit()
    await c.answer()
    await c.message.answer(answer)


@r.callback_query(F.data.startswith("booking:remind:"))
async def remind_guest(c: CallbackQuery):
    booking_id = callback_booking_id(c.data)
    if booking_id is None or c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as s:
        booking, manager = await owned_booking(s, c.from_user.id, booking_id)
        if booking is None:
            return await c.answer("Запись не найдена.", show_alert=True)
        client = await s.get(Client, booking.client_id)
        hotel = await s.get(Hotel, booking.hotel_id)
        await audit(s, manager, "guest_reminder_prepared", "booking", booking.id)
        await s.commit()
    await c.answer()
    await c.message.answer(
        f"🔔 Напоминание гостю\n\n"
        f"Телефон: {client.phone or 'не указан'}\n\n"
        f"Здравствуйте, {client.name}! Напоминаем о фотосъёмке "
        f"{booking.shoot_date:%d.%m.%Y} в {booking.shoot_time:%H:%M}, "
        f"отель «{hotel.name}». Будем вас ждать!"
    )


@r.callback_query(F.data.startswith("booking:reschedule:"))
async def start_reschedule(c: CallbackQuery, state):
    booking_id = callback_booking_id(c.data)
    if booking_id is None or c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as s:
        booking, _manager = await owned_booking(s, c.from_user.id, booking_id)
    if booking is None:
        return await c.answer("Запись не найдена.", show_alert=True)
    await state.set_state(BookingFlow.reschedule_date)
    await state.set_data({"reschedule_booking_id": booking_id})
    await c.answer()
    await c.message.answer("Введите новую дату в формате ДД.ММ.ГГГГ:")


@r.message(BookingFlow.reschedule_date)
async def reschedule_date(m, state):
    try:
        day, month, year = map(int, m.text.strip().split("."))
        value = date(year, month, day)
    except (TypeError, ValueError):
        return await m.answer("Неверная дата. Пример: 21.09.2026")
    await state.update_data(reschedule_date=value.isoformat())
    await state.set_state(BookingFlow.reschedule_time)
    await m.answer("Введите новое время в формате ЧЧ:ММ:")


@r.message(BookingFlow.reschedule_time)
async def finish_reschedule(m, state, current_roles):
    try:
        new_time = time.fromisoformat(m.text.strip())
    except ValueError:
        return await m.answer("Неверное время. Пример: 16:30")
    data = await state.get_data()
    async with Session() as s:
        booking, manager = await owned_booking(
            s, m.from_user.id, data["reschedule_booking_id"], lock=True
        )
        if booking is None:
            await state.clear()
            return await m.answer("Запись не найдена.")
        booking.shoot_date = date.fromisoformat(data["reschedule_date"])
        booking.shoot_time = new_time
        booking.status = "RESCHEDULED"
        shooting = (
            await s.scalars(select(Shooting).where(Shooting.booking_id == booking.id))
        ).one_or_none()
        if shooting:
            shooting.status = "ASSIGNED"
        await audit(s, manager, "booking_rescheduled", "booking", booking.id)
        await s.commit()
    await state.clear()
    await m.answer(
        f"📅 Съёмка #{booking.id} перенесена на {booking.shoot_date:%d.%m.%Y} в {booking.shoot_time:%H:%M}.",
        reply_markup=reply(menu(current_roles)),
    )


@r.message(F.text == "💰 Продажи")
async def sales(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rows = (
            (
                await s.execute(
                    select(Sale)
                    .where(Sale.created_by_id == u.id)
                    .order_by(Sale.created_at.desc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        lines = []
        for sale in rows:
            employee = await s.get(User, sale.credited_user_id)
            lines.append(
                f"Продажа #{sale.id}: {sale.sold_photos} фото = {sale.amount:.2f} ₽, "
                f"начислено сотруднику {employee.name if employee else 'сотрудник удалён'}"
            )
        await m.answer("\n".join(lines) or "Продаж нет.")


@r.message(F.text == "🏆 Премия")
async def bonus(m):
    await m.answer(
        "🏆 Премия: добавляется владельцем/администратором через раздел выплат."
    )


@r.message(F.text == "📊 Статистика")
async def manager_stats(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rev = (
            await s.execute(
                select(func.coalesce(func.sum(Sale.amount), 0)).where(
                    Sale.created_by_id == u.id
                )
            )
        ).scalar() or 0
        await m.answer(f"📊 Оборот оформленных продаж: {rev:.2f} ₽")
