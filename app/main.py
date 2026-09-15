import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import SimpleEventIsolation

from .config import config
from .db import engine, init_db, wait_for_database
from .handlers import admin, common, manager, photographer, sales, training

logger = logging.getLogger(__name__)
READY_FILE = Path(os.getenv("HEALTHCHECK_FILE", "/tmp/photo-boss.ready"))


def create_dispatcher():
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    # Admin filtering must precede the manager's identically named Sales button.
    dispatcher.include_routers(
        common.r, admin.r, photographer.r, manager.r, sales.r, training.r
    )
    return dispatcher


def create_bot():
    session = None
    if config.telegram_api_base:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(config.telegram_api_base, is_local=True)
        )
    return Bot(config.bot_token, session=session)


def clear_ready_file():
    READY_FILE.unlink(missing_ok=True)


def mark_ready(username):
    READY_FILE.parent.mkdir(parents=True, exist_ok=True)
    READY_FILE.write_text(username or "ready", encoding="utf-8")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    clear_ready_file()
    try:
        config.validate()
        await wait_for_database()
        await init_db()
        dispatcher = create_dispatcher()
        bot = create_bot()
        try:
            me = await bot.me()
            logger.info("Telegram bot @%s is authenticated", me.username)
            mark_ready(me.username)
            await dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
                close_bot_session=False,
            )
        finally:
            await bot.session.close()
    finally:
        clear_ready_file()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
