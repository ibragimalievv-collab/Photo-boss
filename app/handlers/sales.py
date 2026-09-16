from decimal import ROUND_HALF_UP, Decimal

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, select

from ..access import StaffFilter
from ..config import config, number
from ..db import Session
from ..models import Booking, Compensation, Package, Photo, Sale, Shooting, User
from ..services.core import audit, get_user, roles_of, setting

r = Router()
r.message.filter(StaffFilter("PHOTOGRAPHER", "MANAGER"), F.text)


class S(StatesGroup):
    booking = State()
    credited = State()
    role = State()
    photos = State()


PHOTO_TIER_THRESHOLD = 150
PHOTO_PERCENT_STANDARD = Decimal(10)
PHOTO_PERCENT_HIGH = Decimal(15)


@r.message(F.text == "🧾 Продажа")
async def begin(m, state):
    async with Session() as session:
        bookings = (
            (
                await session.execute(
                    select(Booking).order_by(Booking.id.desc()).limit(20)
                )
            )
            .scalars()
            .all()
        )
    if not bookings:
        await state.clear()
        return await m.answer("Записей для продажи пока нет.")
    await state.clear()
    await state.set_state(S.booking)
    await m.answer(
        "Введите ID записи:\n"
        + ", ".join(str(booking.id) for booking in bookings)
        + "\nОтмена: /cancel"
    )


@r.message(S.booking)
async def b(m, state):
    try:
        bid = int(m.text)
        if not 0 < bid <= 2**31 - 1:
            raise ValueError
    except (TypeError, ValueError):
        return await m.answer("Нужен положительный числовой ID записи.")
    async with Session() as session:
        booking = await session.get(Booking, bid)
        if booking is None:
            return await m.answer("Запись не найдена.")
        photographer = (
            await session.get(User, booking.photographer_id)
            if booking.photographer_id
            else None
        )
    await state.update_data(booking=bid)
    await state.set_state(S.credited)
    hint = (
        f"\nФотограф записи: {photographer.name}, Telegram ID {photographer.tg_id}."
        if photographer
        else ""
    )
    await m.answer("Введите Telegram ID сотрудника, кому засчитать продажу:" + hint)


@r.message(S.credited)
async def cr(m, state):
    try:
        tg_id = int(m.text)
        if not 0 < tg_id < 2**52:
            raise ValueError
    except (TypeError, ValueError):
        return await m.answer("Нужен положительный Telegram ID.")
    async with Session() as session:
        credited = await get_user(session, tg_id)
        roles = await roles_of(session, credited)
    commission_roles = roles & {"MANAGER", "PHOTOGRAPHER"}
    if not commission_roles:
        return await m.answer(
            "Нужен активный сотрудник с ролью MANAGER или PHOTOGRAPHER."
        )
    await state.update_data(credited=credited.id)
    await state.set_state(S.role)
    names = {"MANAGER": "Менеджер", "PHOTOGRAPHER": "Фотограф"}
    await m.answer(
        "Роль для начисления: "
        + " или ".join(names[role] for role in sorted(commission_roles))
    )


@r.message(S.role)
async def rr(m, state):
    entered = (m.text or "").strip().upper()
    role = {"МЕНЕДЖЕР": "MANAGER", "ФОТОГРАФ": "PHOTOGRAPHER"}.get(
        entered, entered
    )
    data = await state.get_data()
    async with Session() as session:
        credited = await session.get(User, data["credited"])
        roles = await roles_of(session, credited)
        uploaded = await session.scalar(
            select(func.count(Photo.id))
            .join(Shooting, Shooting.id == Photo.shooting_id)
            .where(Shooting.booking_id == data["booking"])
        )
        already_sold = await session.scalar(
            select(func.coalesce(func.sum(Sale.sold_photos), 0)).where(
                Sale.booking_id == data["booking"]
            )
        )
    if role not in {"MANAGER", "PHOTOGRAPHER"} or role not in roles:
        return await m.answer(
            "Выберите назначенную сотруднику роль: Менеджер или Фотограф."
        )
    await state.update_data(role=role)
    await state.set_state(S.photos)
    await m.answer(
        f"Всего готовых фотографий: {uploaded}. Уже куплено: {already_sold}.\n"
        "Сколько фотографий куплено сейчас?"
    )


@r.message(S.photos)
async def save(m, state):
    try:
        count = int(m.text)
        if not 0 < count <= 2**31 - 1:
            raise ValueError
    except (TypeError, ValueError):
        return await m.answer("Введите положительное целое число фото.")
    data = await state.get_data()
    async with Session() as session:
        creator = await get_user(session, m.from_user.id)
        creator_roles = await roles_of(session, creator)
        if not creator_roles & {"OWNER", "ADMIN", "MANAGER", "PHOTOGRAPHER"}:
            await state.clear()
            return await m.answer("Нет доступа.")
        booking = await session.get(Booking, data.get("booking"))
        credited = await session.get(User, data.get("credited"))
        role = data.get("role")
        if (
            booking is None
            or role not in {"MANAGER", "PHOTOGRAPHER"}
            or role not in await roles_of(session, credited)
        ):
            await state.clear()
            return await m.answer(
                "Запись или права сотрудника изменились. Начните продажу заново."
            )
        package = await session.get(Package, booking.package_id)
        if package is None:
            return await m.answer(
                "У записи не найден пакет. Обратитесь к администратору."
            )
        uploaded = await session.scalar(
            select(func.count(Photo.id))
            .join(Shooting, Shooting.id == Photo.shooting_id)
            .where(Shooting.booking_id == booking.id)
        )
        already_sold = await session.scalar(
            select(func.coalesce(func.sum(Sale.sold_photos), 0)).where(
                Sale.booking_id == booking.id
            )
        )
        if not uploaded:
            return await m.answer("Сначала фотограф должен загрузить готовые фотографии.")
        if already_sold + count > uploaded:
            return await m.answer(
                f"Нельзя продать больше загруженных. Всего: {uploaded}, "
                f"уже куплено: {already_sold}, доступно: {uploaded - already_sold}."
            )
        compensations = (
            (
                await session.execute(
                    select(Compensation).where(
                        Compensation.user_id == credited.id, Compensation.role == role
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(compensations) > 1:
            return await m.answer(
                "Найдено несколько настроек комиссии сотрудника. Обратитесь к администратору."
            )
        key = f"{role}_PERCENT"
        default = (
            config.manager_percent if role == "MANAGER" else config.photographer_percent
        )
        percent_value = (
            (
                PHOTO_PERCENT_HIGH
                if await session.scalar(
                    select(func.count(Photo.id))
                    .join(Shooting, Shooting.id == Photo.shooting_id)
                    .join(Booking, Booking.id == Shooting.booking_id)
                    .where(
                        Booking.id == booking.id,
                    )
                )
                >= PHOTO_TIER_THRESHOLD
                else PHOTO_PERCENT_STANDARD
            )
            if role == "PHOTOGRAPHER"
            else (
                compensations[0].sales_percent
                if compensations
                else await setting(session, key, default)
            )
        )
        try:
            price = Decimal(str(number(package.price_per_photo, "Цена", minimum=0.01)))
            percent = Decimal(str(number(percent_value, "Комиссия", maximum=100)))
        except ValueError:
            return await m.answer(
                "Некорректная цена или комиссия. Обратитесь к администратору."
            )
        amount = (count * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        commission = (amount * percent / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        sale = Sale(
            booking_id=booking.id,
            created_by_id=creator.id,
            credited_user_id=credited.id,
            commission_role=role,
            sold_photos=count,
            amount=float(amount),
            percent=float(percent),
            commission=float(commission),
        )
        session.add(sale)
        await session.flush()
        if role == "PHOTOGRAPHER":
            photographed = await session.scalar(
                select(func.count(Photo.id))
                .join(Shooting, Shooting.id == Photo.shooting_id)
                .join(Booking, Booking.id == Shooting.booking_id)
                .where(
                    Booking.id == booking.id,
                )
            )
            day_sales = (
                await session.scalars(
                    select(Sale)
                    .join(Booking, Booking.id == Sale.booking_id)
                    .where(
                        Sale.credited_user_id == credited.id,
                        Sale.commission_role == "PHOTOGRAPHER",
                        Booking.id == booking.id,
                    )
                )
            ).all()
            tier_percent = (
                PHOTO_PERCENT_HIGH
                if photographed >= PHOTO_TIER_THRESHOLD
                else PHOTO_PERCENT_STANDARD
            )
            for item in day_sales:
                item.percent = float(tier_percent)
                item.commission = float(
                    (Decimal(str(item.amount)) * tier_percent / 100).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
            percent = tier_percent
            commission = Decimal(str(sale.commission)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        await audit(
            session,
            creator,
            "sale_created",
            "sale",
            sale.id,
            f"credited={credited.id};role={role}",
        )
        await session.commit()
    await state.clear()
    await m.answer(
        f"Продажа #{sale.id} сохранена. Фотографии: всего {uploaded}, "
        f"куплено {already_sold + count}.\nСумма: {amount:.2f} ₽; "
        f"комиссия {commission:.2f} ₽.\nЗасчитано: {credited.name}."
    )
