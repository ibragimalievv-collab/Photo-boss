from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, select

from ..access import StaffFilter
from ..config import number
from ..db import Session
from ..models import (
    AuditLog,
    Booking,
    Hotel,
    Package,
    PayrollEntry,
    Sale,
    Setting,
    Shooting,
    User,
    UserRole,
)
from ..services.core import audit, get_user, has, roles_of

r = Router()
r.message.filter(StaffFilter("ADMIN"), F.text)


class E(StatesGroup):
    tg = State()
    name = State()
    roles = State()


class H(StatesGroup):
    name = State()


class P(StatesGroup):
    name = State()
    price = State()


async def guard(m, s):
    u = await get_user(s, m.from_user.id)
    rs = await roles_of(s, u)
    return u, has(rs, "ADMIN")


@r.message(F.text == "👥 Сотрудники")
async def employees(m):
    async with Session() as s:
        _user, ok = await guard(m, s)
        if not ok:
            return
        rows = (
            (await s.execute(select(User).order_by(User.id.desc()).limit(50)))
            .scalars()
            .all()
        )
        out = []
        for x in rows:
            rs = await roles_of(s, x)
            out.append(
                f"#{x.id} {x.name} tg:{x.tg_id} [{', '.join(rs)}] {'✅' if x.active else '⛔'}"
            )
        await m.answer("\n".join(out) or "Сотрудников нет.\n\nДобавить: /add_employee")


@r.message(F.text == "🏨 Отели")
async def hotels(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        rows = (await s.execute(select(Hotel))).scalars().all()
        await m.answer(
            "\n".join(f"#{x.id} {x.name} — {x.address or ''}" for x in rows)
            or "Нет отелей."
        )
        await m.answer("Добавить: /add_hotel")


@r.message(F.text == "📦 Пакеты")
async def packages(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        rows = (await s.execute(select(Package))).scalars().all()
        await m.answer(
            "\n".join(f"#{x.id} {x.name}: {x.price_per_photo:.2f} ₽/фото" for x in rows)
            or "Нет пакетов."
        )
        await m.answer("Добавить: /add_package")


@r.message(F.text == "📋 Все записи")
async def allbook(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        n = (await s.execute(select(func.count(Booking.id)))).scalar() or 0
        await m.answer(f"📋 Всего записей: {n}")


@r.message(F.text == "📸 Все съёмки")
async def allshoot(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        rows = (
            (await s.execute(select(Shooting).order_by(Shooting.id.desc()).limit(30)))
            .scalars()
            .all()
        )
        await m.answer("\n".join(f"#{x.id}: {x.status}" for x in rows) or "Нет съёмок.")


@r.message(F.text == "💰 Продажи")
async def allsales(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        total = (
            await s.execute(select(func.coalesce(func.sum(Sale.amount), 0)))
        ).scalar() or 0
        await m.answer(f"💰 Продажи сети: {total:.2f} ₽")


@r.message(F.text == "💵 Зарплаты/выплаты")
async def payroll(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        rows = (
            (
                await s.execute(
                    select(PayrollEntry)
                    .order_by(PayrollEntry.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        await m.answer(
            "\n".join(
                f"{x.period} #{x.user_id} {x.kind}: {x.amount:.2f} ₽" for x in rows
            )
            or "Выплат нет."
        )


@r.message(F.text == "📊 Отчёты")
async def reports(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        sales = (
            await s.execute(select(func.coalesce(func.sum(Sale.amount), 0)))
        ).scalar() or 0
        photos = (
            await s.execute(select(func.coalesce(func.sum(Sale.sold_photos), 0)))
        ).scalar() or 0
        await m.answer(f"📊 Сеть\nПродажи: {sales:.2f} ₽\nПродано фото: {photos}")


@r.message(F.text == "⚙️ Настройки")
async def settings(m):
    await m.answer(
        "Настройки хранятся в БД. Ключи: PHOTO_PRICE, MANAGER_PERCENT, PHOTOGRAPHER_PERCENT. Используйте /set key value."
    )


@r.message(F.text == "📜 Аудит")
async def auditlog(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        rows = (
            (
                await s.execute(
                    select(AuditLog).order_by(AuditLog.created_at.desc()).limit(50)
                )
            )
            .scalars()
            .all()
        )
        await m.answer(
            "\n".join(
                f"{x.created_at:%d.%m %H:%M} #{x.user_id} {x.action} {x.entity or ''}#{x.entity_id or ''}"
                for x in rows
            )
            or "Аудит пуст."
        )


@r.message(F.text == "/add_employee")
async def addemp(m, state):
    await state.clear()
    await state.set_state(E.tg)
    await m.answer("Telegram ID сотрудника (отмена: /cancel):")


@r.message(E.tg)
async def etg(m, state):
    try:
        tg_id = int(m.text)
        if not 0 < tg_id < 2**52:
            raise ValueError
    except (TypeError, ValueError):
        return await m.answer("Введите положительный Telegram ID.")
    await state.update_data(tg=tg_id)
    await state.set_state(E.name)
    await m.answer("Имя:")


@r.message(E.name)
async def ename(m, state):
    name = m.text.strip()
    if not 1 <= len(name) <= 150:
        return await m.answer("Имя: от 1 до 150 символов.")
    await state.update_data(name=name)
    await state.set_state(E.roles)
    await m.answer(
        "Роли через запятую: ADMIN, MANAGER, PHOTOGRAPHER. Назначать ADMIN может только владелец."
    )


@r.message(E.roles)
async def eroles(m, state, current_roles):
    requested = {value.strip().upper() for value in m.text.split(",") if value.strip()}
    if not requested or requested - {"ADMIN", "MANAGER", "PHOTOGRAPHER"}:
        return await m.answer("Допустимы ADMIN, MANAGER, PHOTOGRAPHER.")
    if "ADMIN" in requested and "OWNER" not in current_roles:
        return await m.answer("Назначать администратора может только владелец.")
    data = await state.get_data()
    async with Session() as session:
        actor = await get_user(session, m.from_user.id)
        user = await get_user(session, data["tg"])
        if user is None:
            user = User(tg_id=data["tg"], name=data["name"])
            session.add(user)
            await session.flush()
        existing = set(
            (
                await session.execute(
                    select(UserRole.role).where(UserRole.user_id == user.id)
                )
            ).scalars()
        )
        if existing & {"ADMIN", "OWNER"} and "OWNER" not in current_roles:
            return await m.answer(
                "Изменять владельца или администратора может только владелец."
            )
        user.name = data["name"]
        for role in requested - existing:
            session.add(UserRole(user_id=user.id, role=role))
        await audit(
            session,
            actor,
            "employee_roles_added",
            "user",
            user.id,
            ",".join(sorted(requested - existing)),
        )
        await session.commit()
    await state.clear()
    await m.answer(
        "Сотрудник сохранён. Существующие роли сохранены; для обновления меню сотруднику нужен /start."
    )


@r.message(F.text == "/add_hotel")
async def addhotel(m, state):
    await state.clear()
    await state.set_state(H.name)
    await m.answer("Название отеля (отмена: /cancel):")


@r.message(H.name)
async def hname(m, state):
    name = m.text.strip()
    if not 1 <= len(name) <= 200:
        return await m.answer("Название: от 1 до 200 символов.")
    async with Session() as session:
        hotel = Hotel(name=name)
        session.add(hotel)
        await session.flush()
        await audit(
            session,
            await get_user(session, m.from_user.id),
            "hotel_created",
            "hotel",
            hotel.id,
        )
        await session.commit()
    await state.clear()
    await m.answer("Отель добавлен.")


@r.message(F.text == "/add_package")
async def addpack(m, state):
    await state.clear()
    await state.set_state(P.name)
    await m.answer("Название пакета (отмена: /cancel):")


@r.message(P.name)
async def pname(m, state):
    name = m.text.strip()
    if not 1 <= len(name) <= 150:
        return await m.answer("Название: от 1 до 150 символов.")
    await state.update_data(name=name)
    await state.set_state(P.price)
    await m.answer("Цена за фото:")


@r.message(P.price)
async def pprice(m, state):
    try:
        price = number(m.text, "Цена", minimum=0.01, maximum=1000000000)
    except ValueError:
        return await m.answer(
            "Нужна положительная цена до 1 000 000 000, например 400 или 400.50."
        )
    data = await state.get_data()
    async with Session() as session:
        package = Package(name=data["name"], price_per_photo=price)
        session.add(package)
        await session.flush()
        await audit(
            session,
            await get_user(session, m.from_user.id),
            "package_created",
            "package",
            package.id,
        )
        await session.commit()
    await state.clear()
    await m.answer("Пакет добавлен.")


@r.message(F.text.startswith("/set "))
async def setv(m):
    parts = m.text.split(maxsplit=2)
    if len(parts) != 3:
        return await m.answer("Формат: /set PHOTO_PRICE 400")
    _, key, raw = parts
    key = key.upper()
    if key not in {"PHOTO_PRICE", "MANAGER_PERCENT", "PHOTOGRAPHER_PERCENT"}:
        return await m.answer(
            "Ключ: PHOTO_PRICE, MANAGER_PERCENT или PHOTOGRAPHER_PERCENT."
        )
    try:
        value = number(
            raw,
            key,
            minimum=0.01 if key == "PHOTO_PRICE" else 0,
            maximum=1000000000 if key == "PHOTO_PRICE" else 100,
        )
    except ValueError:
        return await m.answer(
            "Цена должна быть положительной, процент — от 0 до 100. Используйте точку для дробной части."
        )
    async with Session() as session:
        setting = await session.get(Setting, key)
        if setting is None:
            session.add(Setting(key=key, value=str(value)))
        else:
            setting.value = str(value)
        await audit(
            session,
            await get_user(session, m.from_user.id),
            "setting_changed",
            details=f"{key}={value}",
        )
        await session.commit()
    await m.answer(
        "Настройка сохранена."
        + (
            " Цена действует при создании начального базового пакета; цены существующих пакетов не меняются."
            if key == "PHOTO_PRICE"
            else " Новые продажи без персонального процента используют это значение."
        )
    )
