import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, select

from ..access import StaffFilter
from ..config import config, number
from ..db import Session
from ..keyboards import inline
from ..models import (
    AuditLog,
    Booking,
    Client,
    Compensation,
    Hotel,
    Package,
    PayrollEntry,
    Sale,
    Setting,
    Shooting,
    User,
    UserRole,
)
from ..services.bookings import STATUS_NAMES, booking_card
from ..services.core import audit, get_user, has, roles_of
from ..services.training import training_day

r = Router()
r.message.filter(StaffFilter("ADMIN"), F.text)
r.callback_query.filter(StaffFilter("ADMIN"))
logger = logging.getLogger(__name__)

AUDIT_ACTION_NAMES = {
    "booking_created": "Создана запись",
    "booking_confirmed": "Съёмка подтверждена",
    "booking_rejected": "Съёмка отклонена",
    "booking_rescheduled": "Съёмка перенесена",
    "guest_reminder_prepared": "Подготовлено напоминание гостю",
    "sale_created": "Оформлена продажа",
    "premium_added": "Начислена премия",
    "employee_fired": "Сотрудник уволен",
    "employee_restored": "Сотрудник восстановлен",
    "employee_role_removed": "Снята роль сотрудника",
    "shift_started": "Смена начата",
    "shift_finished": "Смена завершена",
    "photos_ready_for_sale": "Фотографии готовы к продаже",
}


class E(StatesGroup):
    tg = State()
    name = State()
    roles = State()


class H(StatesGroup):
    name = State()


class P(StatesGroup):
    name = State()
    price = State()


class PremiumFlow(StatesGroup):
    employee = State()
    amount = State()
    note = State()


class DateLookup(StatesGroup):
    value = State()


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
        f"👤 {user.name}\n"
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
    rows.append([("⬅️ К списку", "employee:list" if user.active else "employee:archive")])
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


async def send_employee_list(message, *, archived=False):
    async with Session() as session:
        users = (
            await session.scalars(
                select(User)
                .where(User.roles.any(), User.active.is_(not archived))
                .order_by(User.name, User.id)
                .limit(50)
            )
        ).all()
    buttons = [[(user.name[:40], f"employee:view:{user.id}")] for user in users]
    if archived:
        buttons.append([("⬅️ Действующие сотрудники", "employee:list")])
    else:
        buttons.append([("➕ Добавить сотрудника", "employee:add")])
        buttons.append([("🗂 Уволенные сотрудники", "employee:archive")])
    await message.answer(
        (
            f"🗂 Уволенные сотрудники: {len(users)}\n\n"
            if archived
            else f"👥 Действующие сотрудники: {len(users)}\n\n"
        )
        + (
            "История сохранена. Сотрудника можно восстановить."
            if archived
            else "Уволенные сотрудники находятся в отдельном разделе."
        ),
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


@r.callback_query(F.data == "employee:archive")
async def employees_archive(callback):
    if callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    await callback.answer()
    await send_employee_list(callback.message, archived=True)


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
        if "PHOTOGRAPHER" in roles:
            future = (
                await session.scalars(
                    select(Booking)
                    .where(
                        Booking.photographer_id == target.id,
                        Booking.status.in_(
                            (
                                "NEW",
                                "PENDING_CONFIRMATION",
                                "CONFIRMED",
                                "ASSIGNED",
                                "RESCHEDULED",
                            )
                        ),
                    )
                    .with_for_update()
                )
            ).all()
            for booking in future:
                booking.photographer_id = None
                booking.status = "CONFIRMED"
                shooting = await session.scalar(
                    select(Shooting).where(Shooting.booking_id == booking.id)
                )
                if shooting and shooting.status in {
                    "ASSIGNED", "PENDING_CONFIRMATION"
                }:
                    shooting.status = "CONFIRMED"
        target.active = False
        target.terminated_at = datetime.now(UTC).replace(tzinfo=None)
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
        target.terminated_at = None
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
        await send_day_picker(m, s, "admin:bookings", 0, "📋 Выберите день записей")


def day_label(value, today):
    if value == today:
        prefix = "Сегодня"
    elif value == today + timedelta(days=1):
        prefix = "Завтра"
    elif value == today + timedelta(days=2):
        prefix = "Послезавтра"
    else:
        prefix = [
            "Понедельник",
            "Вторник",
            "Среда",
            "Четверг",
            "Пятница",
            "Суббота",
            "Воскресенье",
        ][value.weekday()]
    return f"{prefix} — {value:%d.%m.%Y}"


async def send_day_picker(message, session, prefix, offset, title):
    offset = max(0, min(offset, 364))
    today = training_day()
    days = [today + timedelta(days=offset + index) for index in range(7)]
    counts = dict(
        (
            await session.execute(
                select(Booking.shoot_date, func.count(Booking.id))
                .where(Booking.shoot_date.in_(days))
                .group_by(Booking.shoot_date)
            )
        ).all()
    )
    rows = [
        [
            (
                f"{day_label(value, today)} · {counts.get(value, 0)}",
                f"{prefix}:date:{value.isoformat()}",
                "primary",
            )
        ]
        for value in days
    ]
    rows.append([("🔎 Найти по дате", f"{prefix}:search", "success")])
    navigation = []
    if offset:
        navigation.append(("⬅️ Неделя", f"{prefix}:page:{max(0, offset - 7)}"))
    navigation.append(("Неделя ➡️", f"{prefix}:page:{offset + 7}"))
    rows.append(navigation)
    await message.answer(title, reply_markup=inline(rows))


@r.callback_query(
    F.data.in_(
        {
            "admin:bookings:search",
            "admin:shoots:search",
            "admin:report:search",
        }
    )
)
async def start_date_search(c, state):
    mode = c.data.split(":")[1]
    await state.set_state(DateLookup.value)
    await state.set_data({"date_lookup_mode": mode})
    await c.answer()
    await c.message.answer("Введите нужную дату в формате ДД.ММ.ГГГГ:")


@r.callback_query(F.data.startswith("admin:sales:search:"))
async def start_sales_date_search(c, state):
    try:
        hotel_id = int(c.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await c.answer("Некорректный отель.", show_alert=True)
    await state.set_state(DateLookup.value)
    await state.set_data({"date_lookup_mode": "sales", "hotel_id": hotel_id})
    await c.answer()
    await c.message.answer("Введите нужную дату продаж в формате ДД.ММ.ГГГГ:")


@r.message(DateLookup.value)
async def finish_date_search(m, state):
    try:
        day, month, year = map(int, m.text.strip().split("."))
        selected = date(year, month, day)
    except (TypeError, ValueError):
        return await m.answer("Неверная дата. Пример: 15.09.2026")
    data = await state.get_data()
    mode = data.get("date_lookup_mode")
    await state.clear()
    async with Session() as s:
        if mode == "bookings":
            rows = (
                await s.scalars(
                    select(Booking)
                    .where(Booking.shoot_date == selected)
                    .order_by(Booking.shoot_time, Booking.id)
                )
            ).all()
            await m.answer(
                f"📋 ЕЖЕДНЕВНИК\nДата: {selected:%d.%m.%Y}\n"
                f"Съёмок: {len(rows)}\n━━━━━━━━━━━━"
            )
            for booking in rows:
                await m.answer(await booking_card(s, booking))
            report_text, employee_ids = await build_financial_report(
                s, selected, selected + timedelta(days=1)
            )
            buttons = [
                [(employee.name, f"employee:view:{employee.id}", "primary")]
                for user_id in employee_ids
                if (employee := await s.get(User, user_id)) is not None
            ]
            return await m.answer(
                "━━━━━━━━━━━━\n" + report_text,
                reply_markup=inline(buttons) if buttons else None,
            )
        if mode == "shoots":
            rows = (
                await s.execute(
                    select(Booking, Client)
                    .join(Client, Client.id == Booking.client_id)
                    .where(Booking.shoot_date == selected)
                    .order_by(Booking.shoot_time, Booking.id)
                )
            ).all()
            buttons = [
                [
                    (
                        f"{booking.shoot_time:%H:%M} · {client.name}",
                        f"admin:shoot:view:{booking.id}",
                        "primary",
                    )
                ]
                for booking, client in rows
            ]
            return await m.answer(
                f"📸 Съёмки на {selected:%d.%m.%Y}: {len(rows)}",
                reply_markup=inline(buttons) if buttons else None,
            )
        if mode == "report":
            text, employee_ids = await build_financial_report(
                s, selected, selected + timedelta(days=1)
            )
            buttons = [
                [(employee.name, f"employee:view:{employee.id}", "primary")]
                for user_id in employee_ids
                if (employee := await s.get(User, user_id)) is not None
            ]
            return await m.answer(
                text, reply_markup=inline(buttons) if buttons else None
            )
        if mode == "sales":
            return await send_sales_report(
                m, s, data.get("hotel_id", 0), selected, selected + timedelta(days=1)
            )
    await m.answer("Раздел поиска не найден. Откройте меню заново.")


def callback_date(data):
    try:
        return date.fromisoformat(data.rsplit(":", 1)[1])
    except (AttributeError, TypeError, ValueError):
        return None


@r.callback_query(F.data.startswith("admin:bookings:page:"))
async def allbook_page(c):
    try:
        offset = int(c.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as s:
        await send_day_picker(c.message, s, "admin:bookings", offset, "📋 Выберите день записей")
    await c.answer()


@r.callback_query(F.data.startswith("admin:bookings:date:"))
async def allbook_date(c):
    selected = callback_date(c.data)
    if selected is None or c.message is None:
        return await c.answer("Некорректная дата.", show_alert=True)
    async with Session() as s:
        rows = (
            await s.scalars(
                select(Booking)
                .where(Booking.shoot_date == selected)
                .order_by(Booking.shoot_time, Booking.id)
            )
        ).all()
        await c.message.answer(
            f"📋 ЕЖЕДНЕВНИК\nДата: {selected:%d.%m.%Y}\n"
            f"Съёмок: {len(rows)}\n━━━━━━━━━━━━"
        )
        for booking in rows:
            await c.message.answer(await booking_card(s, booking))
        report_text, employee_ids = await build_financial_report(
            s, selected, selected + timedelta(days=1)
        )
        buttons = [
            [(employee.name, f"employee:view:{employee.id}", "primary")]
            for user_id in employee_ids
            if (employee := await s.get(User, user_id)) is not None
        ]
        await c.message.answer(
            "━━━━━━━━━━━━\n" + report_text,
            reply_markup=inline(buttons) if buttons else None,
        )
    await c.answer()


@r.message(F.text == "📸 Все съёмки")
async def allshoot(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        await send_day_picker(m, s, "admin:shoots", 0, "📸 Выберите день съёмок")


@r.callback_query(F.data.startswith("admin:shoots:page:"))
async def allshoot_page(c):
    try:
        offset = int(c.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as s:
        await send_day_picker(c.message, s, "admin:shoots", offset, "📸 Выберите день съёмок")
    await c.answer()


@r.callback_query(F.data.startswith("admin:shoots:date:"))
async def allshoot_date(c):
    selected = callback_date(c.data)
    if selected is None or c.message is None:
        return await c.answer("Некорректная дата.", show_alert=True)
    async with Session() as s:
        rows = (
            await s.execute(
                select(Booking, Client)
                .join(Client, Client.id == Booking.client_id)
                .where(Booking.shoot_date == selected)
                .order_by(Booking.shoot_time, Booking.id)
            )
        ).all()
    buttons = [
        [
            (
                (
                    f"{booking.shoot_time:%H:%M} · {client.name} · "
                    f"{STATUS_NAMES.get(booking.status, 'Статус обновляется')}"
                ),
                f"admin:shoot:view:{booking.id}",
                "primary",
            )
        ]
        for booking, client in rows
    ]
    await c.answer()
    await c.message.answer(
        f"📸 Съёмки на {selected:%d.%m.%Y}: {len(rows)}",
        reply_markup=inline(buttons) if buttons else None,
    )


@r.callback_query(F.data.startswith("admin:shoot:view:"))
async def allshoot_view(c):
    try:
        booking_id = int(c.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as s:
        booking = await s.get(Booking, booking_id)
        if booking is None:
            return await c.answer("Съёмка не найдена.", show_alert=True)
        card = await booking_card(s, booking)
    await c.answer()
    await c.message.answer(card)


@r.message(F.text == "💰 Продажи")
async def allsales(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        hotels = (
            await s.scalars(select(Hotel).where(Hotel.active.is_(True)).order_by(Hotel.name))
        ).all()
        rows = [[("Все отели", "admin:sales:hotel:0", "primary")]]
        rows += [
            [(hotel.name, f"admin:sales:hotel:{hotel.id}", "primary")]
            for hotel in hotels
        ]
        await m.answer(
            "💰 Продажи\nВыберите отель:",
            reply_markup=inline(rows),
        )


async def send_sales_period_picker(message, session, hotel_id, offset=0):
    hotel = await session.get(Hotel, hotel_id) if hotel_id else None
    title = hotel.name if hotel else "Все отели"
    today = training_day()
    offset = max(0, min(offset, 364))
    days = [today - timedelta(days=offset + index) for index in range(7)]
    rows = [
        [
            (
                day_label(value, today),
                f"admin:sales:day:{hotel_id}:{value.isoformat()}",
                "primary",
            )
        ]
        for value in days
    ]
    rows += [
        [("🔎 Найти по дате", f"admin:sales:search:{hotel_id}", "success")],
        [("📅 Текущая неделя", f"admin:sales:period:{hotel_id}:week", "primary")],
        [("🗓 Текущий месяц", f"admin:sales:period:{hotel_id}:month", "primary")],
        [("⬅️ Предыдущие 7 дней", f"admin:sales:page:{hotel_id}:{offset + 7}")],
    ]
    if offset:
        rows.append([("Ближе к сегодня ➡️", f"admin:sales:page:{hotel_id}:{max(0, offset - 7)}")])
    await message.answer(f"🏨 {title}\nВыберите период продаж:", reply_markup=inline(rows))


@r.callback_query(F.data.startswith("admin:sales:hotel:"))
async def sales_hotel(c):
    try:
        hotel_id = int(c.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await c.answer("Некорректный отель.", show_alert=True)
    async with Session() as s:
        if hotel_id and await s.get(Hotel, hotel_id) is None:
            return await c.answer("Отель не найден.", show_alert=True)
        await send_sales_period_picker(c.message, s, hotel_id)
    await c.answer()


@r.callback_query(F.data.startswith("admin:sales:page:"))
async def sales_page(c):
    try:
        _, _, _, raw_hotel, raw_offset = c.data.split(":")
        hotel_id, offset = int(raw_hotel), int(raw_offset)
    except (TypeError, ValueError):
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as s:
        await send_sales_period_picker(c.message, s, hotel_id, offset)
    await c.answer()


async def send_sales_report(message, session, hotel_id, start_date, end_date):
    lower, upper = utc_period(start_date, end_date)
    query = (
        select(Sale)
        .join(Booking, Booking.id == Sale.booking_id)
        .where(Sale.created_at >= lower, Sale.created_at < upper)
        .order_by(Sale.created_at, Sale.id)
    )
    if hotel_id:
        query = query.where(Booking.hotel_id == hotel_id)
    rows = (await session.scalars(query)).all()
    hotel = await session.get(Hotel, hotel_id) if hotel_id else None
    lines = [
        f"💰 Продажи · {hotel.name if hotel else 'Все отели'}",
        f"Период: {start_date:%d.%m.%Y}–{(end_date - timedelta(days=1)):%d.%m.%Y}",
        f"Касса: {sum(item.amount for item in rows):.2f} ₽",
        f"Куплено кадров: {sum(item.sold_photos for item in rows)}",
    ]
    employee_ids = set()
    for item in rows:
        creator = await session.get(User, item.created_by_id)
        credited = await session.get(User, item.credited_user_id)
        employee_ids.update((item.created_by_id, item.credited_user_id))
        lines.append(
            f"\nПродажа #{item.id}: {item.amount:.2f} ₽ · {item.sold_photos} кадров\n"
            f"Оформил: {creator.name}\nНачислено: {credited.name} — "
            f"{item.percent:g}% = {item.commission:.2f} ₽"
        )
    buttons = [
        [(employee.name, f"employee:view:{employee.id}", "primary")]
        for user_id in sorted(employee_ids)
        if (employee := await session.get(User, user_id)) is not None
    ]
    await message.answer(
        "\n".join(lines), reply_markup=inline(buttons) if buttons else None
    )


@r.callback_query(F.data.startswith("admin:sales:day:"))
async def sales_day(c):
    try:
        _, _, _, raw_hotel, raw_date = c.data.split(":")
        hotel_id, selected = int(raw_hotel), date.fromisoformat(raw_date)
    except (TypeError, ValueError):
        return await c.answer("Некорректная дата.", show_alert=True)
    async with Session() as s:
        await send_sales_report(c.message, s, hotel_id, selected, selected + timedelta(days=1))
    await c.answer()


@r.callback_query(F.data.startswith("admin:sales:period:"))
async def sales_period(c):
    try:
        _, _, _, raw_hotel, period = c.data.split(":")
        hotel_id = int(raw_hotel)
    except (TypeError, ValueError):
        return await c.answer("Некорректный период.", show_alert=True)
    today = training_day()
    if period == "week":
        start, end = today - timedelta(days=today.weekday()), None
        end = start + timedelta(days=7)
    elif period == "month":
        start = today.replace(day=1)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    else:
        return await c.answer("Некорректный период.", show_alert=True)
    async with Session() as s:
        await send_sales_report(c.message, s, hotel_id, start, end)
    await c.answer()


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
        lines = []
        employee_ids = set()
        for entry in rows:
            employee = await s.get(User, entry.user_id)
            employee_ids.add(entry.user_id)
            lines.append(
                f"{entry.period} · {employee.name if employee else 'Сотрудник удалён'} · "
                f"{entry.kind}: {entry.amount:.2f} ₽"
            )
        buttons = [[("🏆 Начислить премию", "premium:add", "success")]]
        buttons += [
            [(employee.name, f"employee:view:{employee.id}", "primary")]
            for user_id in sorted(employee_ids)
            if (employee := await s.get(User, user_id)) is not None
        ]
        await m.answer("\n".join(lines) or "Выплат нет.", reply_markup=inline(buttons))


@r.callback_query(F.data == "premium:add")
async def premium_add(c, state):
    async with Session() as s:
        users = (
            await s.scalars(
                select(User).where(User.active.is_(True)).order_by(User.name)
            )
        ).all()
    await state.set_state(PremiumFlow.employee)
    await c.answer()
    await c.message.answer(
        "Выберите сотрудника:",
        reply_markup=inline(
            [[(user.name, f"premium:user:{user.id}")] for user in users]
        ),
    )


@r.callback_query(PremiumFlow.employee, F.data.startswith("premium:user:"))
async def premium_employee(c, state):
    try:
        user_id = int(c.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await c.answer("Некорректный сотрудник.", show_alert=True)
    async with Session() as s:
        user = await s.get(User, user_id)
    if user is None or not user.active:
        return await c.answer("Сотрудник недоступен.", show_alert=True)
    await state.update_data(premium_user_id=user_id)
    await state.set_state(PremiumFlow.amount)
    await c.answer()
    await c.message.answer(f"Введите сумму премии для {user.name}:")


@r.message(PremiumFlow.amount)
async def premium_amount(m, state):
    try:
        amount = float(m.text.strip().replace(",", "."))
        if not 0 < amount <= 10_000_000:
            raise ValueError
    except ValueError:
        return await m.answer("Введите положительную сумму премии.")
    await state.update_data(premium_amount=amount)
    await state.set_state(PremiumFlow.note)
    await m.answer("Введите причину премии:")


@r.message(PremiumFlow.note)
async def premium_note(m, state):
    note = m.text.strip()
    if not 2 <= len(note) <= 500:
        return await m.answer("Введите причину от 2 до 500 символов.")
    data = await state.get_data()
    async with Session() as s:
        actor = await get_user(s, m.from_user.id)
        employee = await s.get(User, data["premium_user_id"])
        if employee is None or not employee.active:
            await state.clear()
            return await m.answer("Сотрудник недоступен.")
        entry = PayrollEntry(
            user_id=employee.id,
            kind="Премия",
            amount=data["premium_amount"],
            period=training_day().isoformat(),
            note=note,
        )
        s.add(entry)
        await s.flush()
        await audit(s, actor, "premium_added", "payroll_entry", entry.id, note)
        await s.commit()
    await state.clear()
    await m.answer(
        f"🏆 Премия {employee.name}: {entry.amount:.2f} ₽. Начисление сохранено."
    )


@r.message(F.text == "📊 Отчёты")
async def reports(m):
    async with Session() as s:
        if not (await guard(m, s))[1]:
            return
        await send_report_picker(m, s, 0)


async def send_report_picker(message, session, offset):
    await send_day_picker(message, session, "admin:report", offset, "📊 Дневной отчёт — выберите день")
    await message.answer(
        "Итоговый период:",
        reply_markup=inline(
            [
                [("📅 Текущая неделя", "admin:report:period:week", "primary")],
                [("🗓 Текущий месяц", "admin:report:period:month", "primary")],
            ]
        ),
    )


@r.callback_query(F.data.startswith("admin:report:page:"))
async def report_page(c):
    try:
        offset = int(c.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as s:
        await send_report_picker(c.message, s, offset)
    await c.answer()


def utc_period(start_date, end_date):
    timezone = ZoneInfo(config.training_timezone)
    start = datetime.combine(start_date, time.min, timezone).astimezone(UTC)
    end = datetime.combine(end_date, time.min, timezone).astimezone(UTC)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


async def build_financial_report(session, start_date, end_date):
    lower, upper = utc_period(start_date, end_date)
    sales = (
        await session.scalars(
            select(Sale).where(Sale.created_at >= lower, Sale.created_at < upper)
        )
    ).all()
    payroll_rows = (
        await session.scalars(
            select(PayrollEntry).where(
                PayrollEntry.created_at >= lower, PayrollEntry.created_at < upper
            )
        )
    ).all()
    compensation = (await session.scalars(select(Compensation))).all()
    users = {user.id: user for user in (await session.scalars(select(User))).all()}

    cash = sum(item.amount for item in sales)
    photos = sum(item.sold_photos for item in sales)
    commissions = sum(item.commission for item in sales)
    payroll_total = sum(item.amount for item in payroll_rows)
    employee_commissions = defaultdict(float)
    employee_sales = defaultdict(float)
    employee_roles = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    employee_payroll = defaultdict(lambda: defaultdict(float))
    salary_settings = defaultdict(float)

    for item in sales:
        employee_commissions[item.credited_user_id] += item.commission
        employee_sales[item.credited_user_id] += item.amount
        role_values = employee_roles[item.credited_user_id][item.commission_role]
        role_values[0] += item.commission
        role_values[1] = item.percent
    for item in payroll_rows:
        employee_payroll[item.user_id][item.kind] += item.amount
    for item in compensation:
        salary_settings[item.user_id] += item.base_salary

    company_total = cash - commissions - payroll_total
    lines = [
        f"📊 Отчёт {start_date:%d.%m.%Y}–{(end_date - timedelta(days=1)):%d.%m.%Y}",
        f"Касса: {cash:.2f} ₽",
        f"Процент сотрудников: {commissions:.2f} ₽",
        f"Начисления/штрафы: {payroll_total:.2f} ₽",
        f"Итого компании: {company_total:.2f} ₽",
        f"Продано кадров: {photos}",
    ]
    employee_ids = sorted(
        set(employee_commissions) | set(employee_payroll) | set(salary_settings),
        key=lambda user_id: users.get(user_id).name if users.get(user_id) else "",
    )
    if employee_ids:
        lines.append("\n👥 Сотрудники:")
    role_names = {"MANAGER": "Менеджер", "PHOTOGRAPHER": "Фотограф"}
    for user_id in employee_ids:
        user = users.get(user_id)
        lines.append(f"\n{user.name if user else f'Сотрудник #{user_id}'}")
        lines.append(f"Продажи: {employee_sales[user_id]:.2f} ₽")
        for role, (amount, percent) in employee_roles[user_id].items():
            lines.append(
                f"{role_names.get(role, role)}: {percent:g}% = {amount:.2f} ₽"
            )
        for kind, amount in employee_payroll[user_id].items():
            lines.append(f"{kind}: {amount:.2f} ₽")
        if salary_settings[user_id]:
            lines.append(f"Установленный оклад: {salary_settings[user_id]:.2f} ₽")
        earned = employee_commissions[user_id] + sum(employee_payroll[user_id].values())
        lines.append(f"Начислено за период: {earned:.2f} ₽")
    return "\n".join(lines), employee_ids


@r.callback_query(F.data.startswith("admin:report:date:"))
async def report_date(c):
    selected = callback_date(c.data)
    if selected is None or c.message is None:
        return await c.answer("Некорректная дата.", show_alert=True)
    async with Session() as s:
        text, employee_ids = await build_financial_report(
            s, selected, selected + timedelta(days=1)
        )
        buttons = [
            [(users_name.name, f"employee:view:{users_name.id}", "primary")]
            for user_id in employee_ids
            if (users_name := await s.get(User, user_id)) is not None
        ]
    await c.answer()
    await c.message.answer(text, reply_markup=inline(buttons) if buttons else None)


@r.callback_query(F.data.startswith("admin:report:period:"))
async def report_period(c):
    today = training_day()
    period = c.data.rsplit(":", 1)[1]
    if period == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = today.replace(day=1)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    else:
        return await c.answer("Некорректный период.", show_alert=True)
    async with Session() as s:
        text, employee_ids = await build_financial_report(s, start, end)
        buttons = [
            [(users_name.name, f"employee:view:{users_name.id}", "primary")]
            for user_id in employee_ids
            if (users_name := await s.get(User, user_id)) is not None
        ]
    await c.answer()
    await c.message.answer(text, reply_markup=inline(buttons) if buttons else None)


@r.message(F.text == "⚙️ Настройки")
async def settings(m):
    await m.answer(
        "⚙️ Настройки: цена фотографии, процент менеджера и правила начисления фотографу."
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
        lines = []
        for entry in rows:
            employee = await s.get(User, entry.user_id) if entry.user_id else None
            lines.append(
                f"{entry.created_at:%d.%m %H:%M} · "
                f"{employee.name if employee else 'Система'} · "
                f"{AUDIT_ACTION_NAMES.get(entry.action, 'Служебное действие')}"
            )
        await m.answer("\n".join(lines) or "Аудит пуст.")


@r.message(F.text.in_({"/add_employee", "➕ Добавить сотрудника"}))
async def addemp(m, state):
    await state.clear()
    await state.set_state(E.tg)
    await m.answer("Введите номер сотрудника в Telegram (отмена: /cancel):")


@r.callback_query(F.data == "employee:add")
async def addemp_button(callback, state):
    if callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    await state.clear()
    await state.set_state(E.tg)
    await callback.message.answer("Введите номер сотрудника в Telegram (отмена: /cancel):")
    await callback.answer()


@r.message(E.tg)
async def etg(m, state):
    try:
        tg_id = int(m.text)
        if not 0 < tg_id < 2**52:
            raise ValueError
    except (TypeError, ValueError):
        return await m.answer("Введите положительный номер сотрудника в Telegram.")
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
        "Роли через запятую: Администратор, Менеджер, Фотограф. "
        "Назначать администратора может только владелец."
    )


@r.message(E.roles)
async def eroles(m, state, current_roles):
    role_aliases = {
        "АДМИНИСТРАТОР": "ADMIN",
        "МЕНЕДЖЕР": "MANAGER",
        "ФОТОГРАФ": "PHOTOGRAPHER",
    }
    requested = {
        role_aliases.get(value.strip().upper(), value.strip().upper())
        for value in m.text.split(",")
        if value.strip()
    }
    if not requested or requested - {"ADMIN", "MANAGER", "PHOTOGRAPHER"}:
        return await m.answer(
            "Допустимые роли: Администратор, Менеджер, Фотограф."
        )
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
