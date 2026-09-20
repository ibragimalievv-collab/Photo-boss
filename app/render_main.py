import asyncio
import contextlib
import logging
import os

from aiogram.exceptions import TelegramAPIError
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import text

from .config import config
from .db import engine, init_db, wait_for_database
from .main import (
    clear_ready_file,
    create_bot,
    create_dispatcher,
    mark_ready,
    operations_loop,
)
from .miniapp_api import install_miniapp
from .miniapp_security import webhook_secret
from .release_policy import app_url
from .services.academy import ACADEMY_LESSONS

logger = logging.getLogger(__name__)
WEBHOOK_PATH = "/telegram/webhook"
RELEASE = "miniapp-v3-test"


async def health(request):
    ready = bool(request.app.get("miniapp_ready", False))
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception:
        ready = False
    return web.json_response(
        {"service": "photo-boss", "status": "ok" if ready else "not_ready",
         "release": RELEASE, "mode": "test",
         "commit": os.getenv("RENDER_GIT_COMMIT", "")[:12]},
        status=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )


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
        secret_token=webhook_secret(bot.token),
        allowed_updates=app["dispatcher"].resolve_used_update_types(),
        drop_pending_updates=False,
    )
    logger.info("Telegram bot @%s is authenticated", me.username)
    logger.info("Protected Telegram webhook configured: %s", url)
    try:
        # This changes the chat menu, not BotFather's separate Main Mini App URL.
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="Photo Boss", web_app=WebAppInfo(url=app_url())
        ))
        logger.info("Telegram chat menu installed: %s", app_url())
    except TelegramAPIError:
        logger.exception("Could not update chat menu; /app remains available")
    app["miniapp_ready"] = True
    mark_ready(me.username)
    app["operations_task"] = asyncio.create_task(operations_loop(bot))
    logger.info("Photo Boss test release ready: %s; Mini App /app/", RELEASE)


async def on_cleanup(app):
    app["miniapp_ready"] = False
    clear_ready_file()
    task = app.get("operations_task")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await app["dispatcher"].storage.close()
    await app["bot"].session.close()
    await engine.dispose()


async def redirect_app(_request):
    raise web.HTTPFound("/app/")


def create_app():
    dispatcher = create_dispatcher()
    bot = create_bot()
    app = web.Application()
    app["bot"] = bot
    app["dispatcher"] = dispatcher
    app["miniapp_ready"] = False
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/app", redirect_app)
    install_miniapp(
        app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS,
        tz_name=config.training_timezone,
    )
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=webhook_secret(bot.token),
        handle_in_background=False,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dispatcher, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = int(os.getenv("PORT", "10000"))
    logger.info("Starting Render test service on port %s", port)
    web.run_app(create_app(), host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
