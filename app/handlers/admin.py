import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, select

from ..access import StaffFilter
from ..config import number
from ..db import Session
from ..keyboards import inline
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
r.callback_query.filter(StaffFilter("ADMIN"))
logger = logging.getLogger(__name__)


class E(StatesGroup):
    tg = State()
    name = State()
    roles = State()


class H(StatesGroup):
    name = State()


class P(StatesGroup):
    name = State()
    price = State()


ROLE_NAMES = {
    "OWNER": "Владелец",
    "ADMIN": "Администратор",
    "MANAGER": "Менеджер",
    "PHOTOGRAPHER": "Фотограф",
}


async def guard(m, s):
    u = await get_user(s, m.from_user.id)
    rs = await roles_of(s, u)
    return u, has(rs, "ADMIN")


async def stored_roles(session, user_id):
    return set(
        (
            await session.scalars(
                select(UserRole.role).where(UserRole.user_id == user_id)
            )
        ).all()
    )


def employee_card(user, roles):
    role_text = ", ".join(ROLE_NAMES.get(role, role) for role in sorted(roles))
    return (
        f"👤 {user.name}\nTelegram ID: {user.tg_id}\n"
        f"Статус: {'работает ✅' if user.active else 'уволен ⛔'}\n"
        f"Роли: {role_text or 'нет ролей'}"
    )


def employee_actions(user, roles, actor_id, actor_roles):
    rows = []
    if user.id != actor_id:
        for role in sorted(roles):
            if role == "OWNER" or (role == "ADMIN" and "OWNER" not in actor_roles):
                continue
            rows.append(
                [
                    (
                        f"➖ Убрать роль: {ROLE_NAMES.get(role, role)}",
                        f"employee:role_remove:{user.id}:{role}",
                    )
                ]
            )
        protected = "OWNER" in roles or (
            "ADMIN" in roles and "OWNER" not in actor_roles
        )
        if not protected:
            if user.active:
                rows.append([("⛔ Уволить", f"employee:fire:{user.id}")])
            elif roles:
                rows.append([("♻️ Восстановить", f"employee:restore:{user.id}")])
    rows.append([("⬅️ К списку", "employee:list")])
    return inline(rows)


async def send_employee_card(message, user_id, actor_id, actor_roles):
    async with Session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return await message.answer("Сотрудник не найден.")
        roles = await stored_roles(session, user.id)
    await message.answer(
        employee_card(user, roles),
        reply_markup=employee_actions(user, roles, actor_id, actor_roles),
    )


async def send_employee_list(message):
    async with Session() as session:
        users = (
            await session.scalars(
                select(User)
                .where(User.roles.any())
                .order_by(User.active.desc(), User.name, User.id)
                .limit(50)
            )
        ).all()
    buttons = [
        [
            (
                f"{'✅' if user.active else '⛔'} {user.name[:32]} (#{user.id})",
                f"employee:view:{user.id}",
            )
        ]
        for user in users
    ]
    buttons.append([("➕ Добавить сотрудника", "employee:add")])
    await message.answer(
        f"👥 Сотрудники: {len(users)}\n\n"
        "Выберите сотрудника, чтобы изменить роли, уволить или восстановить.",
        reply_markup=inline(buttons),
    )


def employee_id(data, prefix):
    try:
        value = int(data.removeprefix(prefix))
        if not 0 < value <= 2**31 - 1:
            raise ValueError
        return value
    except (AttributeError, TypeError, ValueError):
        return None


async def notify_employee(bot, tg_id, text):
    try:
        await bot.send_message(tg_id, text)
    except TelegramAPIError as exc:
        logger.warning("Could not notify employee %s: %s", tg_id, type(exc).__name__)


@r.message(F.text == "👥 Сотрудники")
async def employees(m):
    async with Session() as s:
        _user, ok = await guard(m, s)
        if not ok:
            return
    await send_employee_list(m)


@r.callback_query(F.data == "employee:list")
async def employees_button(callback):
    if callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    await callback.answer()
    await send_employee_list(callback.message)


@r.callback_query(F.data.startswith("employee:view:"))
async def employee_view(callback, current_user, current_roles):
    user_id = employee_id(callback.data, "employee:view:")
    if user_id is None or callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    await callback.answer()
    await send_employee_card(callback.message, user_id, current_user.id, current_roles)


@r.callback_query(F.data.startswith("employee:fire:"))
async def employee_fire(callback, current_user, current_roles):
    user_id = employee_id(callback.data, "employee:fire:")
    if user_id is None or callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        target = await session.get(User, user_id)
        roles = await stored_roles(session, user_id) if target else set()
    if target is None:
        return await callback.answer("Сотрудник не найден.", show_alert=True)
    if target.id == current_user.id or "OWNER" in roles:
        return await callback.answer(
            "Эту учётную запись увольнять нельзя.", show_alert=True
        )
    if "ADMIN" in roles and "OWNER" not in current_roles:
        return await callback.answer(
            "Уволить администратора может только владелец.", show_alert=True
        )
    await callback.answer()
    await callback.message.answer(
        f"Подтвердить увольнение сотрудника «{target.name}»? "
        "Доступ к боту будет отключён, история сохранится.",
        reply_markup=inline(
            [
                [("⛔ Да, уволить", f"employee:fire_confirm:{target.id}")],
                [("Отмена", f"employee:view:{target.id}")],
            ]
        ),
    )


@r.callback_query(F.data.startswith("employee:fire_confirm:"))
async def employee_fire_confirm(callback, current_user, current_roles):
    user_id = employee_id(callback.data, "employee:fire_confirm:")
    if user_id is None or callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        target = await session.get(User, user_id, with_for_update=True)
        roles = await stored_roles(session, user_id) if target else set()
        if target is None:
            return await callback.answer("Сотрудник не найден.", show_alert=True)
        if target.id == current_user.id or "OWNER" in roles:
            return await callback.answer(
                "Эту учётную запись увольнять нельзя.", show_alert=True
            )
        if "ADMIN" in roles and "OWNER" not in current_roles:
            return await callback.answer(
                "Уволить администратора может только владелец.", show_alert=True
            )
        if not target.active:
            return await callback.answer("Сотрудник уже уволен.", show_alert=True)
        target.active = False
        actor = await get_user(session, callback.from_user.id)
        await audit(session, actor, "employee_fired", "user", target.id)
        await session.commit()
        target_tg_id = target.tg_id
        target_name = target.name
    await callback.answer("Сотрудник уволен.")
    await callback.message.answer(
        f"⛔ {target_name} уволен. Доступ отключён, история сохранена.",
        reply_markup=inline([[("⬅️ К списку", "employee:list")]]),
    )
    await notify_employee(
        callback.bot, target_tg_id, "⛔ Ваш доступ к рабочему боту отключён."
    )


@r.callback_query(F.data.startswith("employee:restore:"))
async def employee_restore(callback, current_roles):
    user_id = employee_id(callback.data, "employee:restore:")
    if user_id is None or callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        target = await session.get(User, user_id, with_for_update=True)
        roles = await stored_roles(session, user_id) if target else set()
        if target is None or not roles:
            return await callback.answer(
                "Сотрудник или его роли не найдены.", show_alert=True
            )
        if "OWNER" in roles or ("ADMIN" in roles and "OWNER" not in current_roles):
            return await callback.answer(
                "Восстановить администратора может только владелец.", show_alert=True
            )
        if target.active:
            return await callback.answer("Сотрудник уже работает.", show_alert=True)
        target.active = True
        actor = await get_user(session, callback.from_user.id)
        await audit(session, actor, "employee_restored", "user", target.id)
        await session.commit()
        target_tg_id = target.tg_id
        target_name = target.name
    await callback.answer("Сотрудник восстановлен.")
    await callback.message.answer(
        f"♻️ {target_name} восстановлен. Доступ включён.",
        reply_markup=inline([[("⬅️ К списку", "employee:list")]]),
    )
    await notify_employee(
        callback.bot,
        target_tg_id,
        "♻️ Ваш доступ к рабочему боту восстановлен. Нажмите /start.",
    )


@r.callback_query(F.data.startswith("employee:role_remove:"))
async def employee_remove_role_prompt(callback, current_user, current_roles):
    try:
        _, _, raw_user_id, role = callback.data.split(":", 3)
        user_id = int(raw_user_id)
        if not 0 < user_id <= 2**31 - 1 or role not in ROLE_NAMES:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    if callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        target = await session.get(User, user_id)
        roles = await stored_roles(session, user_id) if target else set()
    if target is None or role not in roles:
        return await callback.answer("Роль уже удалена.", show_alert=True)
    if user_id == current_user.id or "OWNER" in roles:
        return await callback.answer(
            "Роли этой учётной записи защищены.", show_alert=True
        )
    if role == "ADMIN" and "OWNER" not in current_roles:
        return await callback.answer(
            "Убрать администратора может только владелец.", show_alert=True
        )
    await callback.answer()
    await callback.message.answer(
        f"Убрать у сотрудника «{target.name}» роль «{ROLE_NAMES[role]}»?",
        reply_markup=inline(
            [
                [
                    (
                        "➖ Да, убрать роль",
                        f"employee:remove_role:{target.id}:{role}",
                    )
                ],
                [("Отмена", f"employee:view:{target.id}")],
            ]
        ),
    )


@r.callback_query(F.data.startswith("employee:remove_role:"))
async def employee_remove_role(callback, current_user, current_roles):
    try:
        _, _, raw_user_id, role = callback.data.split(":", 3)
        user_id = int(raw_user_id)
        if not 0 < user_id <= 2**31 - 1 or role not in ROLE_NAMES:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    if callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    if user_id == current_user.id:
        return await callback.answer(
            "Нельзя убрать роль у собственной учётной записи.", show_alert=True
        )
    if role == "OWNER":
        return await callback.answer(
            "Роль владельца защищена от удаления.", show_alert=True
        )
    if role == "ADMIN" and "OWNER" not in current_roles:
        return await callback.answer(
            "Убрать администратора может только владелец.", show_alert=True
        )
    async with Session() as session:
        target = await session.get(User, user_id, with_for_update=True)
        target_roles = await stored_roles(session, user_id) if target else set()
        if target is None or role not in target_roles:
            return await callback.answer("Роль уже удалена.", show_alert=True)
        if "OWNER" in target_roles:
            return await callback.answer(
                "Роли владельца защищены от изменения.", show_alert=True
            )
        user_role = (
            await session.scalars(
                select(UserRole).where(
                    UserRole.user_id == target.id, UserRole.role == role
                )
            )
        ).one()
        await session.delete(user_role)
        remaining = target_roles - {role}
        if not remaining:
            target.active = False
        actor = await get_user(session, callback.from_user.id)
        await audit(
            session,
            actor,
            "employee_role_removed",
            "user",
            target.id,
            role,
        )
        await session.commit()
        target_tg_id = target.tg_id
        target_name = target.name
    await callback.answer("Роль удалена.")
    await callback.message.answer(
        f"➖ У сотрудника {target_name} удалена роль «{ROLE_NAMES[role]}»."
        + (" Доступ отключён: ролей не осталось." if not remaining else ""),
        reply_markup=inline([[("⬅️ К списку", "employee:list")]]),
    )
    await notify_employee(
        callback.bot,
        target_tg_id,
        f"➖ У вас удалена роль «{ROLE_NAMES[role]}». Нажмите /start, чтобы обновить меню.",
    )


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


@r.message(F.text.in_({"/add_employee", "➕ Добавить сотрудника"}))
async def addemp(m, state):
    await state.clear()
    await state.set_state(E.tg)
    await m.answer("Telegram ID сотрудника (отмена: /cancel):")


@r.callback_query(F.data == "employee:add")
async def addemp_button(callback, state):
    if callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    await state.clear()
    await state.set_state(E.tg)
    await callback.message.answer("Telegram ID сотрудника (отмена: /cancel):")
    await callback.answer()


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
        user.active = True
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
