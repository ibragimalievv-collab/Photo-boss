import logging
import os

from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .config import config
from .db import engine, init_db, wait_for_database
from .main import clear_ready_file, create_bot, create_dispatcher, mark_ready

logger = logging.getLogger(__name__)
WEBHOOK_PATH = "/telegram/webhook"


async def health(_request):
    return web.json_response({"service": "photo-boss", "status": "ok"})


def webhook_url():
    explicit = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return f"{explicit}{WEBHOOK_PATH}"
    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "photo-boss.onrender.com").strip()
    return f"https://{hostname}{WEBHOOK_PATH}"


async def on_startup(app):
    clear_ready_file()
    config.validate()
    await wait_for_database()
    await init_db()

    bot = app["bot"]
    me = await bot.me()
    url = webhook_url()
    await bot.set_webhook(
        url=url,
        allowed_updates=app["dispatcher"].resolve_used_update_types(),
        drop_pending_updates=False,
    )
    logger.info("Telegram bot @%s is authenticated", me.username)
    logger.info("Telegram webhook configured: %s", url)
    mark_ready(me.username)


async def on_cleanup(app):
    clear_ready_file()
    await app["bot"].session.close()
    await engine.dispose()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dispatcher = create_dispatcher()
    bot = create_bot()

    app = web.Application()
    app["bot"] = bot
    app["dispatcher"] = dispatcher
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dispatcher, bot=bot)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    port = int(os.getenv("PORT", "10000"))
    logger.info("Starting Render web service on port %s", port)
    web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
