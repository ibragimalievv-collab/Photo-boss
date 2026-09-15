from datetime import date, time

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline, reply
from ..models import Booking, Client, Hotel, Package, Sale, Shooting, User, UserRole
from ..services.core import audit, get_user, menu

r = Router()
r.message.filter(StaffFilter("MANAGER"), F.text)
r.callback_query.filter(StaffFilter("MANAGER"))


class BookingFlow(StatesGroup):
    hotel = State()
    client_name = State()
    client_phone = State()
    room = State()
    shoot_date = State()
    shoot_time = State()
    package = State()
    photographer = State()


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
    await state.set_state(BookingFlow.room)
    await m.answer("Введите номер комнаты:")


@r.message(BookingFlow.room)
async def booking_room(m, state):
    room = m.text.strip()
    if not room or len(room) > 100:
        return await m.answer("Введите корректный номер комнаты.")
    await state.update_data(room=room)
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
            shoot_date=date.fromisoformat(data["shoot_date"]),
            shoot_time=time.fromisoformat(data["shoot_time"]),
            package_id=data["package_id"], manager_id=manager.id,
            photographer_id=photographer_id,
            status="ASSIGNED" if photographer_id else "NEW",
        )
        s.add(booking)
        await s.flush()
        s.add(Shooting(booking_id=booking.id, status="ASSIGNED"))
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
            await s.execute(
                select(Booking, Hotel, Client)
                .join(Hotel, Hotel.id == Booking.hotel_id)
                .join(Client, Client.id == Booking.client_id)
                .where(Booking.manager_id == u.id)
                .order_by(Booking.shoot_date.desc())
                .limit(30)
            )
        ).all()
        await m.answer(
            "\n".join(
                f"#{b.id} {h.name} / {c.name} / {b.shoot_date} {b.shoot_time} / {b.status}"
                for b, h, c in rows
            )
            or "Записей нет."
        )


@r.message(F.text == "📸 Съёмки")
async def shootings(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rows = (
            await s.execute(
                select(Booking, Shooting)
                .join(Shooting, Shooting.booking_id == Booking.id)
                .where(Booking.manager_id == u.id)
            )
        ).all()
        await m.answer(
            "\n".join(f"#{b.id}: {sh.status}" for b, sh in rows) or "Съёмок нет."
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
        await m.answer(
            "\n".join(
                f"#{x.id}: {x.sold_photos} фото = {x.amount:.2f} ₽, засчитано #{x.credited_user_id}"
                for x in rows
            )
            or "Продаж нет."
        )


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
