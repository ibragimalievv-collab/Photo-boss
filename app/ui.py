import asyncio
import logging
from collections import defaultdict

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendDocument, SendMediaGroup, SendMessage, SendPhoto
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
)

logger = logging.getLogger(__name__)


class CompactChat:
    """Keep private bot chats tidy by replacing the previous interaction screen."""

    def __init__(self):
        self._messages = defaultdict(set)
        self._locks = defaultdict(asyncio.Lock)

    async def remember(self, result):
        messages = result if isinstance(result, list) else [result]
        for message in messages:
            if isinstance(message, Message) and message.chat.type == "private":
                self._messages[message.chat.id].add(message.message_id)

    async def clear(self, bot, chat_id):
        async with self._locks[chat_id]:
            message_ids = self._messages.pop(chat_id, set())
            if not message_ids:
                return
            # Telegram accepts no more than 100 identifiers per request.
            ordered = sorted(message_ids)
            for start in range(0, len(ordered), 100):
                try:
                    await bot.delete_messages(chat_id, ordered[start : start + 100])
                except TelegramAPIError as exc:
                    logger.debug("Не удалось очистить сообщения чата %s: %s", chat_id, exc)


compact_chat = CompactChat()


def add_navigation(method):
    markup = getattr(method, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }
    if callbacks & {"nav:back", "nav:home"}:
        return
    labels = {
        button.text for row in markup.inline_keyboard for button in row
    }
    navigation = []
    if not any(label.startswith("⬅️") or label == "Отмена" for label in labels):
        navigation.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:back")
        )
    navigation.append(
        InlineKeyboardButton(text="🏠 Главная", callback_data="nav:home")
    )
    method.reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            *markup.inline_keyboard,
            navigation,
        ]
    )


def should_preserve(method):
    if isinstance(method, (SendPhoto, SendDocument, SendMediaGroup)):
        return True
    if not isinstance(method, SendMessage):
        return False
    text = method.text or ""
    return text.startswith(
        ("📊 Отчёт", "📋 ЕЖЕДНЕВНИК", "💰 Продажи ·", "✅ ", "⛔ ")
    )


class CompactUiMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data):
        if not getattr(data["bot"], "_compact_ui_enabled", False):
            return await handler(event, data)
        source = event.callback_query or event.message
        if source is not None:
            message = source.message if isinstance(source, CallbackQuery) else source
            if message is not None and message.chat.type == "private":
                await compact_chat.clear(
                    data["bot"],
                    message.chat.id,
                )
        return await handler(event, data)


async def remember_sent_message(make_request, bot, method):
    add_navigation(method)
    result = await make_request(bot, method)
    # The message carrying the permanent bottom menu is the chat's anchor.
    # Removing it makes Telegram show the "Start bot" screen again.
    if isinstance(method, SendMessage) and isinstance(
        method.reply_markup, ReplyKeyboardMarkup
    ):
        return result
    if not should_preserve(method) and isinstance(
        method, (SendMessage, SendPhoto, SendDocument, SendMediaGroup)
    ):
        await compact_chat.remember(result)
    return result


def enable_compact_ui(bot):
    bot._compact_ui_enabled = True
    bot.session.middleware(remember_sent_message)
