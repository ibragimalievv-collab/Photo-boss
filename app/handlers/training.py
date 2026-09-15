from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile

from ..access import StaffFilter
from ..keyboards import inline
from ..services.core import ROLES
from ..services.training import CATEGORY_BY_SLUG, category_rows

r = Router()
r.message.filter(StaffFilter(*ROLES), F.text)
r.callback_query.filter(StaffFilter(*ROLES))


@r.message(F.text == "🎓 Обучение")
async def training_menu(message):
    await message.answer(
        "🎓 Обучение\n\n"
        "Выберите категорию. Бот пришлёт один эталонный кадр — его нужно повторить.",
        reply_markup=inline(category_rows()),
    )


@r.callback_query(F.data.startswith("training:"))
async def training_reference(callback: CallbackQuery):
    slug = callback.data.partition(":")[2]
    category = CATEGORY_BY_SLUG.get(slug)
    if category is None:
        return await callback.answer("Категория не найдена.", show_alert=True)
    if callback.message is None or not category.image_path.is_file():
        return await callback.answer("Фотография временно недоступна.", show_alert=True)
    await callback.message.answer_photo(
        FSInputFile(category.image_path),
        caption=f"{category.title}\n\n📸 Повторите этот кадр.",
    )
    await callback.answer()
