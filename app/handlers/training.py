from aiogram import Router, F

r = Router()

@r.message(F.text == '🎓 Обучение')
async def training_menu(message):
    await message.answer(
        '🎓 Обучение\n\n'
        'Сегодня: 5 новых поз для практики.\n'
        'После загрузки работы бот проверит применение поз и даст рекомендации.'
    )

