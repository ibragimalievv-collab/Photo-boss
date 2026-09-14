import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation

from .config import config
from .db import engine, init_db
from .handlers import admin, common, manager, photographer, sales


def create_dispatcher():
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    # Admin filtering must precede the manager's identically named Sales button.
    dispatcher.include_routers(common.r, admin.r, photographer.r, manager.r, sales.r)
    return dispatcher


async def main():
    config.validate()
    logging.basicConfig(level=logging.INFO)
    try:
        await init_db()
        async with Bot(config.bot_token) as bot:
            await create_dispatcher().start_polling(bot)
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
