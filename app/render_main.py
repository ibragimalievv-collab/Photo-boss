import asyncio
import logging
import os

from aiohttp import web

from .config import config
from .db import engine, init_db, wait_for_database
from .main import clear_ready_file, create_bot, create_dispatcher, mark_ready

logger = logging.getLogger(__name__)


async def health(_request):
    return web.json_response({"service": "photo-boss", "status": "ok"})


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    clear_ready_file()
    runner = None
    try:
        config.validate()
        await wait_for_database()
        await init_db()

        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv("PORT", "10000"))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logger.info("Render health endpoint listening on port %s", port)

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
        if runner is not None:
            await runner.cleanup()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
