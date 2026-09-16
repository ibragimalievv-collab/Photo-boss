from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ReplyKeyboardRemove

from ..config import config
from ..db import Session
from ..keyboards import reply
from ..services.core import ROLES, bootstrap, get_user, menu, roles_of

r = Router()
r.message.filter(F.chat.type == "private", F.from_user)


@r.message(CommandStart())
async def start(m, state):
    await state.clear()
    async with Session() as session:
        user = await bootstrap(
            session,
            m.from_user.id,
            m.from_user.full_name,
            m.from_user.username,
            m.from_user.id in config.admin_ids,
        )
        roles = await roles_of(session, user)
    if not roles:
        return await m.answer(
            f"Доступ не назначен или отключён. Обратитесь к администратору.\n"
            f"Ваш номер в системе: {m.from_user.id}",
            reply_markup=ReplyKeyboardRemove(),
        )
    await m.answer(
        "🏨 PHOTO BOSS\n\nРоли: "
        + ", ".join(ROLES.get(role, role) for role in sorted(roles)),
        reply_markup=reply(menu(roles)),
    )


@r.message(Command("myid"))
async def myid(m):
    await m.answer(str(m.from_user.id))


@r.message(Command("cancel"))
@r.message(F.text == "❌ Отменить")
async def cancel(m, state):
    await state.clear()
    async with Session() as session:
        user = await get_user(session, m.from_user.id)
        roles = await roles_of(session, user)
    await m.answer("Действие отменено." if roles else "Доступ отключён.")


@r.callback_query(F.data.in_({"nav:back", "nav:home"}))
async def navigation(callback: CallbackQuery, state):
    await state.clear()
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            "🏠 Главное меню\n\nВыберите нужный раздел кнопками внизу."
        )
