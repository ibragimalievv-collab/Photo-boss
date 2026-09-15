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
            [
                InlineKeyboardButton(
                    text=item[0],
                    callback_data=item[1],
                    style=item[2] if len(item) > 2 else None,
                )
                for item in row
            ]
            for row in rows
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
