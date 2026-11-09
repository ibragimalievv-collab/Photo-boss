"""Standalone Photo Boss: real web handlers, explicit workers, no Telegram startup."""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def prepare_environment():
    if os.getenv('PHOTO_BOSS_STANDALONE') != '1':
        raise ValueError('PHOTO_BOSS_STANDALONE=1 is required for this entry')
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.dont_write_bytecode = True
    for name in tuple(os.environ):
        if (name in {'BOT_TOKEN', 'TELEGRAM_API_BASE', 'EXPECTED_BOT_ID', 'HR_BOT_TOKEN'}
                or name.startswith(('TELEGRAM_', 'RENDER_'))
                or any(marker in name for marker in ('RESET_REQUEST', 'POINT_SEED', 'TEXT_REQUEST'))):
            os.environ.pop(name, None)
    os.environ.update(PHOTO_BOSS_NOTIFICATION_CHANNEL='app', MINIAPP_SET_MENU='0',
                      AI_STARTUP_PROBE='0', HR_BOT_ENABLED='0')


def origin():
    value = os.getenv('WEBHOOK_BASE_URL', '').rstrip('/')
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment):
        raise ValueError('A canonical HTTPS application origin is required')
    return value


async def verify_readiness(engine, configuration):
    """Read schema and existing owners; never run legacy init_db/seeds/resets."""
    import asyncio
    import uuid

    from sqlalchemy import inspect, text

    from . import models  # noqa: F401 - register the real schema, without DDL.
    from .db import Base

    async with asyncio.timeout(30), engine.connect() as connection:
        if connection.dialect.name == 'postgresql':
            await connection.execute(text("SET LOCAL statement_timeout='5000ms'"))
            safe_role = (await connection.execute(text(
                'SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole '
                'FROM pg_roles WHERE rolname=current_user'))).scalar()
            if safe_role is not True:
                raise RuntimeError('standalone_application_database_role_required')
        def schema(conn):
            inspector = inspect(conn)
            existing = set(inspector.get_table_names())
            for table in Base.metadata.sorted_tables:
                if table.name not in existing:
                    return False
                if not set(table.columns.keys()) <= {c['name'] for c in inspector.get_columns(table.name)}:
                    return False
            return True
        if not await connection.run_sync(schema):
            raise RuntimeError('reviewed_standalone_schema_preparation_required')
        from .refund_schema import verify_ready as verify_refund_schema
        await verify_refund_schema(connection)
        owners = (await connection.execute(text(
            "SELECT count(DISTINCT u.id) FROM users u JOIN user_roles r ON r.user_id=u.id "
            "WHERE u.active=TRUE AND r.role='OWNER'"))).scalar()
        if not owners:
            raise RuntimeError('existing_active_owner_required_no_automatic_bootstrap')
        if configuration.operational_start_date:
            epochs = dict((await connection.execute(text(
                "SELECT key,value FROM settings WHERE key IN ('operations:generation','sales:generation')"))).all())
            values = [epochs.get(k) for k in ('operations:generation', 'sales:generation')]
            try:
                valid = all(isinstance(v, str) and str(uuid.UUID(v)) == v for v in values)
            except (ValueError, TypeError):
                valid = False
            if not valid or values[0] == values[1]:
                raise RuntimeError('prepared_new_period_generations_required')


def create_app(*, start_workers=False, engine=None, readiness=verify_readiness, workers=None):
    prepare_environment()
    if type(start_workers) is not bool:
        raise ValueError('Explicit worker mode required')
    from aiohttp import web

    from .config import config
    from .standalone_runtime import (
        LocalAppContext,
        start_worker_tasks,
        stop_worker_tasks,
        worker_map,
    )

    if not config.standalone:
        raise ValueError('Configuration was imported before standalone mode')
    config.validate()
    origin()
    if engine is None:
        from .db import engine
    context = LocalAppContext(config)
    from .request_diagnostics import request_diagnostics

    @web.middleware
    async def standalone_boundary(request, handler):
        if request.path.startswith(('/telegram/', '/auth/browser/')):
            return web.json_response({'error': 'Войдите по персональному QR или логину и паролю.'},
                                     status=404, headers={'Cache-Control': 'no-store'})
        if request.path in {'/app/', '/app/index.html'} and request.method in {'GET', 'HEAD'}:
            html = (Path(__file__).parent / 'webapp/index.html').read_text()
            html = html.replace('<script src="https://telegram.org/js/telegram-web-app.js"></script>', '')
            html = re.sub(r'(<p id="installLoginHint"[^>]*>).*?(</p>)',
                r'\1После установки войдите по персональному QR или логину и паролю.\2', html, count=1)
            html = html.replace(' В Telegram используй меню «Открыть в браузере».', '')
            return web.Response(text=html, content_type='text/html', headers={'Cache-Control': 'no-store',
                'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})
        return await handler(request)

    app = web.Application(client_max_size=25 * 1024 * 1024,
                          middlewares=[standalone_boundary, request_diagnostics])
    app['bot'], app['ready'] = context, False
    app['standalone'] = True
    app['worker_map'] = worker_map(config, enabled=start_workers)
    from .miniapp_api import install_miniapp
    from .services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
    api = install_miniapp(app, engine=engine, bot=context, lessons=ACADEMY_LESSONS,
        blocks=ACADEMY_BLOCKS, tz_name=config.training_timezone, standalone=True,
        qr_signing_key=bytes.fromhex(config.qr_signing_key))
    from .academy_practice import install_academy_practice
    from .attendance import install_attendance
    from .browser_login import install_browser_login
    from .people import install_people
    from .work_calls import install_work_calls
    from .work_chat import install_work_chat
    from .yandex_disk import install_yandex_disk
    install_browser_login(app, api)
    install_attendance(app, api)
    install_people(app, api)
    install_work_calls(app, install_work_chat(app, api))
    install_academy_practice(app, api)
    install_yandex_disk(app)
    # The legacy Avito lifetime calls prepare(): DDL and a default control seed.
    # A native release consumes the separately prepared schema/control instead.
    avito = None
    if all(os.getenv(k, '').strip() for k in ('AVITO_CLIENT_ID', 'AVITO_CLIENT_SECRET')):
        from .avito_hr import Worker
        avito = Worker(engine)
        app['standalone_avito'] = avito
        app.router.add_get('/health/avito', avito.health)
    contexts = list(app.cleanup_ctx)
    app.cleanup_ctx.clear()
    # Route-declared contexts are gated with the same readiness check as jobs.
    async def lifetime(_app):
        try:
            await readiness(engine, config)
            async with AsyncExitStack() as stack:
                if start_workers:
                    if avito is not None:
                        from sqlalchemy import text
                        async with engine.connect() as connection:
                            control = (await connection.execute(text(
                                'SELECT id FROM hr_avito_control WHERE id=1'))).scalar()
                        if control != 1:
                            raise RuntimeError('prepared_avito_control_required_no_automatic_seed')
                        avito.ready = True
                    for installed in contexts:
                        await stack.enter_async_context(asynccontextmanager(installed)(app))
                    app['worker_map']['realtime_expiry'] = 'context_started'
                    await start_worker_tasks(app, api, engine, config, context, factories=workers)
                app['ready'] = True
                yield
                app['ready'] = False
                await stop_worker_tasks(app)
        finally:
            app['ready'] = False
            await stop_worker_tasks(app)
            if avito is not None:
                avito.ready = False
            await context.session.close()
            await engine.dispose()
    app.cleanup_ctx.append(lifetime)

    async def health(_request):
        import asyncio

        from sqlalchemy import text
        ready = (bool(app['ready']) and 'failed' not in app['worker_map'].values()
                 and context.blocked_telegram_calls == 0)
        if start_workers and avito is not None:
            app['worker_map']['avito_hr'] = 'running' if avito.ready else 'failed'
            ready = ready and avito.ready
        try:
            async with asyncio.timeout(5), engine.connect() as connection:
                await connection.execute(text('SELECT 1'))
        except Exception:  # noqa: BLE001 -- readiness probe reports false
            ready = False
        storage = app['yandex_disk'].public_status()
        return web.json_response({'service': 'photo-boss', 'mode': 'standalone',
            'status': 'ok' if ready else 'not_ready', 'telegramEnabled': False,
            'blockedTelegramCalls': context.blocked_telegram_calls,
            'backgroundWorkersEnabled': start_workers, 'workers': app['worker_map'],
            'notifications': {'channel': 'app', 'pushConfigured': bool(
                os.getenv('WEB_PUSH_PUBLIC_KEY') and os.getenv('WEB_PUSH_PRIVATE_KEY') and os.getenv('WEB_PUSH_SUBJECT'))},
            'storage': {'configured': bool(storage.get('configured')),
                        'connected': bool(storage.get('connected'))},
            'backup': app['standalone_backups'].public_status() if 'standalone_backups' in app else {
                'localVerified': False, 'diskVerified': False, 'restoreRehearsed': False}},
            status=200 if ready else 503, headers={'Cache-Control': 'no-store'})

    async def redirect(_request):
        raise web.HTTPPermanentRedirect(location='/app/')
    app.router.add_get('/', redirect)
    app.router.add_get('/app', redirect)
    app.router.add_get('/health', health)
    if any(name in sys.modules for name in ('app.main', 'app.render_main')):
        raise RuntimeError('telegram_production_startup_import_forbidden')
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check-only', action='store_true')
    modes.add_argument('--serve', action='store_true')
    parser.add_argument('--workers', action='store_true', help='Enable the reviewed native business worker map')
    args = parser.parse_args()
    prepare_environment()
    from .config import config
    config.validate()
    origin()
    if args.check_only:
        import json
        print(json.dumps({'configured': True, 'standalone': True, 'telegramEnabled': False,
                          'databaseTouched': False, 'workersStarted': False}))
        return
    from aiohttp import web
    web.run_app(create_app(start_workers=args.workers), host='127.0.0.1',
                port=int(os.getenv('PORT', '18082')), print=None, access_log=None)


if __name__ == '__main__':
    main()
