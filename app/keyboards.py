from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def reply(items):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=x) for x in items[i : i + 2]]
            for i in range(0, len(items), 2)
        ],
        resize_keyboard=True,
    )


def inline(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows
        ]
    )


def request_location():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📍 Поделиться местоположением", request_location=True
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
