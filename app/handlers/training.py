from aiogram import F, Router

from ..access import StaffFilter
from ..services.core import ROLES
from ..services.training import daily_training_text

r = Router()
r.message.filter(StaffFilter(*ROLES), F.text)


@r.message(F.text == "🎓 Обучение")
async def training_menu(message):
    await message.answer(daily_training_text())
