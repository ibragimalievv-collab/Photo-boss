import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import SimpleEventIsolation

from .config import config
from .db import Session, engine, init_db, wait_for_database
from .handlers import (
    academy,
    admin,
    common,
    manager,
    operations,
    photographer,
    receipts,
    sales,
    training,
)
from .services.photo_storage import storage_loop
from .ui import CompactUiMiddleware, enable_compact_ui
from .yandex_disk import YandexDisk, configured_from_env

logger = logging.getLogger(__name__)
READY_FILE = Path(os.getenv("HEALTHCHECK_FILE", "/tmp/photo-boss.ready"))


async def operations_loop(bot, interval=300):
    from .services.operations import maybe_send_daily_backup, run_operations_once
    while True:
        try:
            async with Session() as session:
                result = await run_operations_once(session, bot)
                result["backups"] = await maybe_send_daily_backup(session, bot)
                await session.commit()
            if any(result.values()):
                logger.info("Operations cycle: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Operations cycle failed")
            for tg_id in config.admin_ids:
                with contextlib.suppress(Exception):
                    await bot.send_message(
                        tg_id, "🚨 Photo Boss: фоновый процесс завершился с ошибкой. "
                        "Проверьте логи Render; следующая попытка будет автоматически.",
                    )
        await asyncio.sleep(interval)


def create_dispatcher():
    if config.redis_url:
        from aiogram.fsm.storage.redis import RedisStorage
        storage = RedisStorage.from_url(config.redis_url, state_ttl=86400, data_ttl=86400)
        dispatcher = Dispatcher(storage=storage, events_isolation=storage.create_isolation())
    else:
        dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    from .audit_context import ActorMiddleware
    dispatcher.update.outer_middleware(ActorMiddleware())
    dispatcher.update.outer_middleware(CompactUiMiddleware())
    dispatcher.include_routers(
        common.r, receipts.r, admin.r, photographer.r, manager.r, sales.r,
        operations.r, academy.r, training.r,
    )
    return dispatcher


def create_bot():
    session = None
    if config.telegram_api_base:
        session = AiohttpSession(api=TelegramAPIServer.from_base(config.telegram_api_base, is_local=True))
    bot = Bot(config.bot_token, session=session, default=DefaultBotProperties(protect_content=True))
    enable_compact_ui(bot)
    return bot


def clear_ready_file():
    READY_FILE.unlink(missing_ok=True)


def mark_ready(username):
    READY_FILE.parent.mkdir(parents=True, exist_ok=True)
    READY_FILE.write_text(username or "ready", encoding="utf-8")


async def main():
    from .launch_policy import install_launch_policies
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    clear_ready_file()
    try:
        config.validate()
        await wait_for_database()
        await init_db()
        dispatcher = create_dispatcher()
        bot = create_bot()
        install_launch_policies(dispatcher, bot)
        token, client_id = configured_from_env()
        storage = YandexDisk(token, client_id)
        if token:
            await storage.verify(write_test=True)
        from .development import development_loop
        development_task = asyncio.create_task(development_loop(engine,bot))
        operations_task = asyncio.create_task(operations_loop(bot))
        storage_task = asyncio.create_task(storage_loop(bot, storage)) if token else None
        try:
            me = await bot.me()
            logger.info("Telegram bot @%s is authenticated", me.username)
            mark_ready(me.username)
            await dispatcher.start_polling(
                bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False,
            )
        finally:
            development_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await development_task
            operations_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operations_task
            if storage_task:
                storage_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await storage_task
            await dispatcher.storage.close()
            await bot.session.close()
    finally:
        clear_ready_file()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
