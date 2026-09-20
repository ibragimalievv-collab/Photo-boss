"""Production entry point: one bot, authenticated webhook and live Mini App."""
import asyncio
import contextlib
import logging
import os

from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import text

from .attendance import install_attendance
from .config import config
from .db import engine, init_db, wait_for_database
from .launch_policy import app_url, install_launch_policies, reset_chat_menu
from .main import (
    clear_ready_file,
    create_bot,
    create_dispatcher,
    mark_ready,
    operations_loop,
)
from .miniapp_api import install_miniapp
from .miniapp_security import webhook_secret
from .services.academy import ACADEMY_LESSONS

logger = logging.getLogger(__name__)
WEBHOOK_PATH = "/telegram/webhook"
RELEASE = "miniapp-3.2-attendance"


async def health(request):
    ready = bool(request.app.get("ready", False))
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Database readiness check failed")
        ready = False
    return web.json_response({"service": "photo-boss", "status": "ok" if ready else "not_ready",
                              "release": RELEASE, "app": "/app/"}, status=200 if ready else 503,
                             headers={"Cache-Control": "no-store"})


def webhook_url():
    return app_url().split("/app/", 1)[0] + WEBHOOK_PATH


async def on_startup(app):
    clear_ready_file()
    app["ready"] = False
    config.validate()
    await wait_for_database()
    await init_db()
    bot = app["bot"]
    me = await bot.me()
    expected = os.getenv("EXPECTED_BOT_ID", "").strip()
    if expected and str(me.id) != expected:
        raise RuntimeError("Unexpected bot identity; deployment stopped")
    await bot.set_webhook(
        url=webhook_url(), secret_token=webhook_secret(config.bot_token),
        allowed_updates=app["dispatcher"].resolve_used_update_types(),
        drop_pending_updates=False,
    )
    if os.getenv("MINIAPP_SET_MENU", "1") == "1":
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Photo Boss", web_app=WebAppInfo(url=app_url())))
        for tg_id in config.admin_ids:
            await reset_chat_menu(bot, tg_id)
        logger.info("Mini App menu configured: %s", app_url())
    logger.info("Telegram bot @%s is authenticated", me.username)
    logger.info("Authenticated Telegram webhook configured")
    app["operations_task"] = asyncio.create_task(operations_loop(bot))
    app["ready"] = True
    mark_ready(me.username)
    logger.info("Release %s ready; live Mini App enabled; demo data disabled", RELEASE)


async def on_cleanup(app):
    clear_ready_file()
    app["ready"] = False
    task = app.get("operations_task")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await app["dispatcher"].storage.close()
    await app["bot"].session.close()
    await engine.dispose()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dispatcher, bot = create_dispatcher(), create_bot()
    install_launch_policies(dispatcher, bot)
    app = web.Application(client_max_size=1024*1024)
    app["bot"], app["dispatcher"] = bot, dispatcher
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    miniapp = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, tz_name=config.training_timezone)
    install_attendance(app, miniapp)
    SimpleRequestHandler(
        dispatcher=dispatcher, bot=bot, secret_token=webhook_secret(config.bot_token),
        handle_in_background=False,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dispatcher, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    port = int(os.getenv("PORT", "10000"))
    logger.info("Starting Render web service on port %s", port)
    web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
