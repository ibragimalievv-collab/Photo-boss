"""Runtime policy for legacy bot entry points during Mini App rollout.

The API and the old chat commands must enforce the same boundaries. Financial
chat navigation is deliberately routed to the authenticated Mini App instead of
leaving historical aggregate callbacks available to administrators.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from urllib.parse import urlsplit

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
    ReplyKeyboardMarkup, WebAppInfo,
)
from sqlalchemy import select

from .config import config
from .db import Session, engine
from .miniapp_api import ACTIONS, MiniApp, as_utc
from .miniapp_security import role_permissions
from .models import AuditLog, Booking, User
from .services.core import get_user, roles_of

logger = logging.getLogger(__name__)
FINANCE_TEXTS = frozenset({"💰 Продажи", "📊 Отчёты", "💵 Зарплаты/выплаты"})


def app_url(page="home"):
    base = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
    if not base:
        host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "photo-boss.onrender.com").strip()
        base = "https://" + host
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Mini App requires a trusted HTTPS base URL")
    if page not in {"home", "finance", "audit", "academy", "schedule"}:
        page = "home"
    return base + "/app/#" + page


def app_markup(page="home"):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📱 Открыть Photo Boss", web_app=WebAppInfo(url=app_url(page)))
    ]])


def safe_menu(markup, roles):
    """Remove obsolete duplicate training and non-owner audit menu entries."""
    if not isinstance(markup, ReplyKeyboardMarkup):
        return markup
    rows = []
    for row in markup.keyboard:
        kept = [button for button in row if button.text != "🎓 Обучение"
                and (button.text != "📜 Аудит" or "OWNER" in roles)]
        if kept:
            rows.append(kept)
    return markup.model_copy(update={"keyboard": rows})


async def protect_navigation(make_request, bot, method):
    markup = getattr(method, "reply_markup", None)
    if isinstance(markup, ReplyKeyboardMarkup):
        chat_id = getattr(method, "chat_id", None)
        async with Session() as session:
            user = await get_user(session, chat_id) if isinstance(chat_id, int) else None
            roles = await roles_of(session, user)
        method.reply_markup = safe_menu(markup, roles)
    return await make_request(bot, method)


async def reset_chat_menu(bot, chat_id):
    await bot.set_chat_menu_button(
        chat_id=chat_id,
        menu_button=MenuButtonWebApp(text="Photo Boss", web_app=WebAppInfo(url=app_url())),
    )


async def respond(event, text, page=None):
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except TelegramAPIError:
            pass
    if message is not None:
        return await message.answer(text, reply_markup=app_markup(page) if page else None,
                                    protect_content=True)


class LaunchPolicy(BaseMiddleware):
    """Inner middleware: StaffFilter has already authenticated every event."""
    async def __call__(self, handler, event, data):
        user, roles = data.get("current_user"), set(data.get("current_roles", ()))
        callback = data.get("handler")
        function = getattr(callback, "callback", None)
        module = getattr(function, "__module__", "")
        name = getattr(function, "__name__", "")
        message = event.message if isinstance(event, CallbackQuery) else event
        if user is None or not roles or message is None:
            return await handler(event, data)
        text_value = getattr(event, "text", "") or ""
        callback_value = getattr(event, "data", "") or ""
        state = data.get("state")
        state_data = await state.get_data() if state else {}

        if module.endswith(".admin"):
            if name == "auditlog":
                if "OWNER" not in roles:
                    return await respond(event, "Аудит доступен только владельцу.")
                return await self.audit(event)
            finance = (text_value in FINANCE_TEXTS
                       or callback_value.startswith(("admin:sales:", "admin:report:"))
                       or (name == "finish_date_search" and state_data.get("date_lookup_mode") in {"sales", "report"}))
            if finance:
                if state:
                    await state.clear()
                return await self.finance(event, user, roles)
            # These legacy booking views also appended a full financial report.
            if name == "allbook_date" or (name == "finish_date_search" and state_data.get("date_lookup_mode") == "bookings"):
                try:
                    if name == "allbook_date":
                        selected = date.fromisoformat(callback_value.rsplit(":", 1)[1])
                    else:
                        day, month, year = map(int, text_value.strip().split("."))
                        selected = date(year, month, day)
                except (ValueError, TypeError):
                    return await respond(event, "Некорректная дата. Используйте ДД.ММ.ГГГГ.")
                if state:
                    await state.clear()
                return await self.bookings(event, selected)

        if module.endswith(".sales") and not roles & {"OWNER", "ADMIN"}:
            from .handlers.sales import S, ask_commission_role
            if name == "begin":
                async with Session() as session:
                    ids = (await session.scalars(select(Booking.id).where(
                        (Booking.photographer_id == user.id) | (Booking.manager_id == user.id)
                    ).order_by(Booking.id.desc()).limit(20))).all()
                await state.clear()
                if not ids:
                    return await respond(event, "Ваших записей для продажи пока нет.")
                await state.set_state(S.booking)
                return await respond(event, "Введите номер вашей записи:\n" + ", ".join(map(str, ids)) + "\nОтмена: /cancel")
            booking_id = state_data.get("booking")
            if name == "b":
                try:
                    booking_id = int(text_value)
                    if not 0 < booking_id < 2**31:
                        raise ValueError
                except (ValueError, TypeError):
                    return await respond(event, "Введите положительный номер вашей записи.")
            if booking_id is not None:
                async with Session() as session:
                    booking = await session.get(Booking, booking_id)
                if booking is None or user.id not in {booking.photographer_id, booking.manager_id}:
                    await state.clear()
                    return await respond(event, "Нет доступа к этой записи. Продажи доступны только по вашим съёмкам и записям.")
            if name == "b":
                await state.update_data(booking=booking_id)
                return await ask_commission_role(message, state, user, roles)
            credited = state_data.get("credited")
            if name == "credit_button":
                try:
                    credited = int(callback_value.rsplit(":", 1)[1])
                except ValueError:
                    credited = None
            if (credited is not None and credited != user.id) or name == "cr":
                await state.clear()
                return await respond(event, "Засчитывать продажи другому сотруднику может только администратор или владелец.")

        if module.endswith(".training") and text_value == "🎓 Обучение":
            from .handlers.academy import academy_home
            return await academy_home(message, roles)
        return await handler(event, data)

    async def finance(self, event, user, roles):
        bot = event.bot
        api = MiniApp(engine, bot, [], tz_name=config.training_timezone)
        today = api.today()
        actor = {"id": user.id, "roles": list(roles), "permissions": role_permissions(roles)}
        async with engine.connect() as conn:
            result = await api.finance_data(conn, actor, today, today, "today")
        money = lambda amount: f"{amount / 100:,.2f}".replace(",", " ") + " ₽"
        text_value = (f"📊 Отчёт за сегодня · {today:%d.%m.%Y}\n\n"
                      f"Подтверждённая касса: {money(result['cashReceived'])}\n"
                      f"Продажи: {money(result['sales'])}\n"
                      f"Начислено команде: {money(result['payroll'])}\n\n"
                      "Начислено — не выплачено. Продажи — не обязательно полученные деньги.\n")
        text_value += ("Выберите любой период в приложении." if "OWNER" in roles
                       else "Администратору доступна общая касса только за сегодня. История и аудит закрыты.")
        return await respond(event, text_value, "finance")

    async def bookings(self, event, selected):
        from .services.bookings import booking_card
        async with Session() as session:
            rows = (await session.scalars(select(Booking).where(Booking.shoot_date == selected)
                                          .order_by(Booking.shoot_time, Booking.id).limit(30))).all()
            cards = [await booking_card(session, item) for item in rows]
        await respond(event, f"📋 Записи на {selected:%d.%m.%Y}\nФинансовые отчёты доступны отдельно в приложении.")
        for card in cards:
            await respond(event, card)
        if not cards:
            await respond(event, "Записей нет.")
        elif len(cards) == 30:
            await respond(event, "Показаны первые 30 записей. Полный список — в приложении.", "home")

    async def audit(self, event):
        from zoneinfo import ZoneInfo
        async with Session() as session:
            rows = (await session.execute(select(AuditLog, User.name).outerjoin(User, User.id == AuditLog.user_id)
                                          .order_by(AuditLog.id.desc()).limit(15))).all()
        lines = ["📜 Аудит · только владелец"]
        for entry, name in rows:
            at = as_utc(entry.created_at).astimezone(ZoneInfo(config.training_timezone))
            title = ACTIONS.get(entry.action, f"Событие «{entry.action}»")
            detail = entry.details or "Дополнительные сведения не были записаны."
            lines.append(f"{at:%d.%m %H:%M} · {name or 'Система'}\n{title}\n"
                         f"Объект: {entry.entity or '—'} #{entry.entity_id or '—'}\n{detail}")
        text_value = "\n\n".join(lines)
        for offset in range(0, len(text_value), 3500):
            await respond(event, text_value[offset:offset+3500])
        return await respond(event, "Полная лента без ограничения на последние 15 записей — в приложении.", "audit")


def install_launch_policies(dispatcher, bot):
    """Both runtime entry points call this; isolated core handler tests need not."""
    if getattr(dispatcher, "_launch_policy_installed", False):
        return
    from .handlers import admin, sales, training
    for router in (admin.r, sales.r, training.r):
        router.message.middleware(LaunchPolicy())
        router.callback_query.middleware(LaunchPolicy())
    bot.session.middleware(protect_navigation)
    dispatcher._launch_policy_installed = True
