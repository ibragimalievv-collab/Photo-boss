"""Production entry point: one bot, authenticated webhook and live Mini App."""
import asyncio
import contextlib
import logging
import os

from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import text

from .academy_practice import install_academy_practice
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
from .people import install_people
from .services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from .services.photo_storage import storage_loop
from .work_calls import ice_config, install_work_calls
from .work_chat import cleanup_expired_chat, cleanup_loop, install_work_chat
from .yandex_disk import install_yandex_disk, storage_recovery_loop

logger = logging.getLogger(__name__)
WEBHOOK_PATH = "/telegram/webhook"
RELEASE = "miniapp-3.38-disk-sale-sources"


async def health(request):
    ready = bool(request.app.get("ready", False))
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Database readiness check failed")
        ready = False
    storage = request.app.get("yandex_disk")
    storage_state = storage.public_status() if storage is not None else {"configured": False, "connected": False, "writeVerified": False}
    calls_state = ice_config(0)
    from .push_notifications import app_channel
    from .push_notifications import configured as push_configured
    return web.json_response({"service": "photo-boss", "status": "ok" if ready else "not_ready",
                              "release": RELEASE, "commit": os.getenv("RENDER_GIT_COMMIT", "")[:12] or None, "app": "/app/",
                              "notifications": {"channel": "app" if app_channel() else "telegram", "pushConfigured": push_configured()},
                              "calls": {"enabled": "work_calls" in request.app,
                                        "maxParticipants": calls_state["maxParticipants"],
                                        "relayConfigured": calls_state["relayConfigured"]},
                              "storage": {"provider": "yandex_disk",
                                          "configured": bool(storage_state.get("configured")),
                                          "connected": bool(storage_state.get("connected")),
                                          "writeVerified": bool(storage_state.get("writeVerified")),
                                          "readVerified": bool(storage_state.get("readVerified")),
                                          "checkedAt": storage_state.get("checkedAt"),
                                          "fullCheckedAt": storage_state.get("fullCheckedAt"),
                                          "errorStage": storage_state.get("errorStage"),
                                          "errorCode": storage_state.get("errorCode"),
                                          "providerStatus": storage_state.get("providerStatus")}},
                             status=200 if ready else 503, headers={"Cache-Control": "no-store"})


def webhook_url():
    return app_url().split("/app/", 1)[0] + WEBHOOK_PATH


async def on_startup(app):
    clear_ready_file()
    app["ready"] = False
    config.validate()
    await wait_for_database()
    await init_db()
    from .operations_reset_startup import REQUEST_ENV, startup_reset
    await startup_reset(engine, os.getenv(REQUEST_ENV, ''), config.admin_ids)
    from .sales_reset import REQUEST_ENV as SALES_RESET_ENV
    from .sales_reset import startup_sales_reset
    # Preserve pre-reset counts and actual folder evidence before cancellation.
    if os.getenv(SALES_RESET_ENV, '').strip():
        from .storage_inventory import log_sales_storage_audit
        await log_sales_storage_audit(engine, app['yandex_disk'])
    await startup_sales_reset(engine, os.getenv(SALES_RESET_ENV, ''), config.admin_ids)
    from .hotel_points import REVIEW_ENV, SEED_ENV, review_required, startup_hotel_point
    await startup_hotel_point(engine, os.getenv(SEED_ENV, ''), config.admin_ids)
    logger.info('Attendance protection: manual_review=%s flag=%s', review_required(), REVIEW_ENV)
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
    logger.info("Academy AI configured=%s; model=%s; tracks=photographer,booking", bool(config.openai_api_key), config.academy_analysis_model)
    if os.getenv("AI_STARTUP_PROBE", "0") == "1":
        from .services.development_ai import ROLEPLAY_SCHEMA, structured
        await structured('Проверка подключения. Верни reply="Готов", score=null, errors=[], recommendations=[].',
            [{'type': 'input_text', 'text': 'Синтетический тест без личных данных.'}],
            ROLEPLAY_SCHEMA, 'startup_probe', limit=300)
    if config.openai_api_key:
        async def verify_live_voice():
            from .realtime_training import model_name, probe_realtime
            available = await probe_realtime()
            logger.info('Realtime API configuration check: model=%s ready=%s', model_name(), available)
        app['realtime_probe_task'] = asyncio.create_task(verify_live_voice())
    logger.info("Authenticated Telegram webhook configured")
    storage_status = await app["yandex_disk"].verify(write_test=True)
    if not storage_status["connected"] or not storage_status["writeVerified"] or not storage_status["readVerified"]:
        logger.warning("Yandex.Disk storage is degraded; Telegram workflows remain available")
    app["storage_recovery_task"] = asyncio.create_task(storage_recovery_loop(app["yandex_disk"]))
    deleted = await cleanup_expired_chat(engine, app["yandex_disk"])
    if deleted:
        logger.info("Expired work-chat messages removed on startup: %s", deleted)
    if review_required():
        from datetime import datetime, timezone

        from .attendance_fines import fine_loop, initialize, worker_api
        fine_api = worker_api(engine, config.training_timezone)
        await initialize(fine_api, datetime.now(timezone.utc))
        app['attendance_fine_task'] = asyncio.create_task(fine_loop(fine_api))
    app["operations_task"] = asyncio.create_task(operations_loop(bot))
    from .bank_payments import payment_loop
    app['bank_payment_task'] = asyncio.create_task(payment_loop(engine))
    app["storage_task"] = asyncio.create_task(storage_loop(bot, app["yandex_disk"]))
    from .services.assignment_notifications import assignment_loop
    app['assignment_notification_task'] = asyncio.create_task(assignment_loop(engine, bot))
    from .services.booking_reminders import booking_reminder_loop
    app['booking_reminder_task'] = asyncio.create_task(booking_reminder_loop(engine))
    from .push_notifications import log_push_state, push_loop
    await log_push_state(engine)
    app['push_notification_task'] = asyncio.create_task(push_loop(engine))
    from .storage_inventory import log_recent_inventory
    app['storage_inventory_task'] = asyncio.create_task(log_recent_inventory(engine, app['yandex_disk']))
    app["chat_cleanup_task"] = asyncio.create_task(cleanup_loop(engine, app["yandex_disk"]))
    app["academy_review_task"] = asyncio.create_task(app["academy_practice"].worker(app))
    from .development import development_loop
    app["development_task"] = asyncio.create_task(development_loop(engine, bot))
    from .disk_folder_audit import disk_audit_loop
    app['disk_folder_audit_task'] = asyncio.create_task(disk_audit_loop(engine))
    app["ready"] = True
    mark_ready(me.username)
    logger.info("Release %s ready; live Mini App enabled; demo data disabled", RELEASE)


async def on_cleanup(app):
    clear_ready_file()
    app["ready"] = False
    for key in ("disk_folder_audit_task", "booking_reminder_task", "attendance_fine_task", "push_notification_task", "bank_payment_task", "realtime_probe_task", "operations_task", "storage_task", "storage_recovery_task", "storage_inventory_task", "chat_cleanup_task", "academy_review_task", "development_task", "assignment_notification_task"):
        task = app.get(key)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    await app["dispatcher"].storage.close()
    await app["bot"].session.close()
    from .operations_guard import close_operation_leases
    await close_operation_leases(engine)
    await engine.dispose()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dispatcher, bot = create_dispatcher(), create_bot()
    install_launch_policies(dispatcher, bot)
    app = web.Application(client_max_size=25*1024*1024)
    app["bot"], app["dispatcher"] = bot, dispatcher
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    miniapp = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS,
                              blocks=ACADEMY_BLOCKS, tz_name=config.training_timezone)
    from .browser_login import install_browser_login
    install_browser_login(app, miniapp)
    install_attendance(app, miniapp)
    install_people(app, miniapp)
    chat = install_work_chat(app, miniapp)
    install_work_calls(app, chat)
    install_academy_practice(app, miniapp)
    install_yandex_disk(app)
    SimpleRequestHandler(
        dispatcher=dispatcher, bot=bot, secret_token=webhook_secret(config.bot_token),
        handle_in_background=False,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dispatcher, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    from .hr_bot import install_hr_bot
    install_hr_bot(app, engine, config.bot_token)
    from .avito_hr import install_avito_hr
    install_avito_hr(app, engine)
    port = int(os.getenv("PORT", "10000"))
    logger.info("Starting Render web service on port %s", port)
    web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
