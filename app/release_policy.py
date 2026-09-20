"""Release guardrails for existing Telegram screens.

These are INNER middleware on the admin/sales routers, after StaffFilter has
resolved current_user/current_roles. Old buttons cannot bypass Mini App policy.
"""
import os
from urllib.parse import urlsplit

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from sqlalchemy import select

from .access import StaffFilter
from .db import Session
from .models import Booking


def app_url(section="home"):
    base = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
    if not base:
        host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "photo-boss.onrender.com").strip()
        base = f"https://{host}"
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Mini App requires an HTTPS origin without credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("WEBHOOK_BASE_URL must be an origin, not a path")
    if section not in {"home", "shootings", "schedule", "academy", "finance", "audit", "profile"}:
        raise ValueError("Unknown Mini App section")
    return f"{base}/app/#{section}"


def app_keyboard(section="home"):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть Photo Boss", web_app=WebAppInfo(url=app_url(section)))
    ]])


def legacy_destination(roles, *, text="", callback="", mode=None):
    """Return None to keep original behaviour, otherwise a safe destination."""
    roles = set(roles)
    if text == "📜 Аудит" or callback.startswith("admin:audit"):
        return "audit" if "OWNER" in roles else "denied"
    if "OWNER" in roles or "ADMIN" not in roles:
        return None
    if text in {"💰 Продажи", "💵 Зарплаты/выплаты", "📊 Отчёты"}:
        return "finance"
    if text == "📋 Все записи":
        return "shootings"
    if callback.startswith(("admin:sales:", "admin:report:")):
        return "finance"
    if callback.startswith("admin:bookings:"):
        return "shootings"
    if mode in {"sales", "report"}:
        return "finance"
    if mode == "bookings":
        return "shootings"
    return None


async def respond(event, text, *, section=None):
    markup = app_keyboard(section) if section else None
    if isinstance(event, CallbackQuery):
        await event.answer()
        message = event.message
        if message:
            await message.answer(text, reply_markup=markup, protect_content=True)
    else:
        await event.answer(text, reply_markup=markup, protect_content=True)


class LegacyFinanceGuard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        roles = data.get("current_roles", set())
        state = data.get("state")
        mode = None
        if state is not None:
            current = await state.get_state()
            if current and current.startswith("DateLookup:"):
                mode = (await state.get_data()).get("date_lookup_mode")
        target = legacy_destination(
            roles,
            text=getattr(event, "text", "") or "",
            callback=getattr(event, "data", "") or "",
            mode=mode,
        )
        if target is None:
            return await handler(event, data)
        if state is not None:
            await state.clear()
        if target == "denied":
            return await respond(event, "Аудит доступен только владельцу.")
        messages = {
            "audit": "📜 Подробный аудит находится в приложении: действие, сотрудник, объект и сохранённые подробности.",
            "finance": "💰 Администратору доступна общая касса только за сегодня. Откройте защищённый отчёт в приложении.",
            "shootings": "📋 Записи доступны в приложении. Историческая общая касса в старых карточках администратора закрыта.",
        }
        return await respond(event, messages[target], section=target)


class OwnBookingSalesGuard(BaseMiddleware):
    """Prevent staff selecting an unrelated booking through old sale FSM steps."""
    async def __call__(self, handler, event, data):
        roles, actor = data.get("current_roles", set()), data.get("current_user")
        if {"OWNER", "ADMIN"} & set(roles):
            return await handler(event, data)
        if actor is None:
            return await respond(event, "Рабочая роль не назначена.")
        state = data.get("state")
        current = await state.get_state() if state is not None else None
        if getattr(event, "text", "") == "🧾 Продажа":
            if state is not None:
                await state.clear()
            async with Session() as session:
                bookings = (await session.scalars(
                    select(Booking)
                    .where((Booking.photographer_id == actor.id) | (Booking.manager_id == actor.id))
                    .order_by(Booking.id.desc()).limit(20)
                )).all()
            if not bookings:
                return await respond(event, "У вас нет назначенных или записанных съёмок для продажи.")
            if state is not None:
                from .handlers.sales import S
                await state.set_state(S.booking)
            return await respond(event, "Введите номер вашей записи:\n" + ", ".join(str(b.id) for b in bookings) + "\nОтмена: /cancel")
        booking_id = None
        if current == "S:booking":
            try:
                booking_id = int(getattr(event, "text", ""))
            except (TypeError, ValueError):
                return await handler(event, data)
        elif current and current.startswith("S:") and state is not None:
            booking_id = (await state.get_data()).get("booking")
        if booking_id is not None:
            async with Session() as session:
                booking = await session.get(Booking, booking_id) if 0 < booking_id < 2**31 else None
            if booking is None or actor.id not in {booking.photographer_id, booking.manager_id}:
                if state is not None:
                    await state.clear()
                return await respond(event, "Нет доступа к этой записи. Продажу можно оформить только по своей съёмке или записи.")
        return await handler(event, data)


def make_miniapp_router():
    router = Router(name="miniapp_launcher")
    router.message.filter(StaffFilter("OWNER", "ADMIN", "MANAGER", "PHOTOGRAPHER"))

    async def launch(message, state):
        await state.clear()
        section = "academy" if message.text == "🎓 Обучение" else "home"
        await message.answer(
            "📱 Photo Boss · тестовый выпуск\n\n"
            "Приложение подключено к рабочему учёту. График, уроки и оформление сохраняются. "
            "На бесплатном сервере первый запуск после паузы может быть медленным.",
            reply_markup=app_keyboard(section), protect_content=True,
        )

    router.message.register(launch, Command("app"))
    router.message.register(launch, F.text.in_({"📱 Приложение", "🎓 Обучение"}))
    return router


def install_legacy_guards(admin_router, sales_router):
    # create_dispatcher may be called by isolated tests; never install twice.
    if not getattr(admin_router, "_miniapp_guards", False):
        admin_router.message.middleware(LegacyFinanceGuard())
        admin_router.callback_query.middleware(LegacyFinanceGuard())
        admin_router._miniapp_guards = True
    if not getattr(sales_router, "_miniapp_guards", False):
        sales_router.message.middleware(OwnBookingSalesGuard())
        sales_router.callback_query.middleware(OwnBookingSalesGuard())
        sales_router._miniapp_guards = True
