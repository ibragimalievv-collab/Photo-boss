"""Explicitly armed native entry for the complete Photo Boss QR production app.

--check-only verifies staged code/assets without importing the application.
--arm-production additionally requires PHOTO_BOSS_PRODUCTION_ARMED=1 and a
protected, complete runtime environment. Only then may the original application
register Telegram webhooks, change menus, write storage or run its workers.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import sys
from urllib.parse import quote, quote_plus, urlsplit


BASE = Path(__file__).resolve().parents[1]
SOURCE = BASE / 'source'
MANIFEST = BASE / 'source-manifest.json'
SDK = Path(__file__).resolve().parent / 'trial-assets/telegram-web-app.js'
RUNTIME_ENV = Path('/etc/photoboss-production/runtime.env')
ORIGIN = 'https://photo-boss.ru'
OLD_ORIGIN = 'https://photo-boss.onrender.com'
AUTH_ORIGINS = {
    'photo-boss.ru': ORIGIN,
    '217.198.13.9:18443': 'https://217.198.13.9:18443',
}
PORT = 18082
SDK_SHA256 = '124bdff7ba3fe86c8ba5143467afec21e020405e74abdb92cfdda6c0ee3b5908'
BASE_COMMIT = '25bb29db6b4628ba9c9c361d5f2e93d60422ef48'
REQUIRED_SOURCE = frozenset({
    'app/render_main.py', 'app/staff_qr_login.py', 'app/miniapp_sessions.py',
    'app/miniapp_api.py', 'app/people.py', 'app/people_ui/people.js',
    'app/people_ui/people.css', 'app/staff_qr_ui/index.html',
    'app/staff_qr_ui/login.js', 'app/staff_qr_ui/login.css',
    'app/staff_qr_ui/scan.js', 'app/staff_qr_ui/scan.css',
    'app/staff_qr_ui/decoder.js', 'app/staff_qr_ui/decoder-LICENSE.txt',
    'app/staff_qr_ui/decoder-source.json', 'app/webapp/index.html',
    'app/webapp/js/account.js', 'app/webapp/js/voice-training.js',
    'app/config.py', 'app/operational_start.py', 'app/bank_payments.py',
    'app/merchant_qr.py', 'app/realtime_training.py', 'app/voice_training.py',
    'app/yandex_realtime.py', 'app/yandex_voice.py',
    'app/services/ai_provider.py', 'app/services/development_ai.py',
    'app/services/training_ai.py', 'app/services/receipts.py',
    'app/webapp/sw.js', 'app/webapp/js/app.js', 'app/webapp/js/outbox.js',
    'app/webapp/js/workflow.js', 'app/webapp/js/bank-payments.js',
    'app/webapp/js/merchant-qr.js', 'app/webapp/js/realtime-training.js',
    'app/webapp/js/voice-pcm-worklet.js',
    'app/standalone_main.py', 'app/standalone_runtime.py', 'app/standalone_backup.py',
    'app/payment_mode.py', 'app/gallery_reserve.py',
    'app/webapp/js/booking-receipt.js', 'app/webapp/js/gallery-reserve.js', 'app/webapp/js/gallery-sale.js',
})
RESET_COMMANDS = (
    'PHOTO_BOSS_OPERATIONS_RESET_REQUEST',
    'PHOTO_BOSS_SALES_FINES_RESET_REQUEST',
    'PHOTO_BOSS_WORK_HISTORY_RESET_REQUEST',
    'PHOTO_BOSS_HOTEL_POINT_SEED',
    'PHOTO_BOSS_SCHEDULE_TEXT_REQUEST',
)
LOGIN_FORM = '<form id="passwordLoginForm" class="form-grid">'
OLD_LOGIN_BUTTON = ('<button class="btn ghost" data-action="browser-login">'
                    'Первый вход через Telegram</button>')
LOGIN_ENTRY = ('<button class="btn primary" data-action="browser-login">'
               'Войти через Telegram</button>'
               '<p class="small">Подтвердите вход в Telegram и вернитесь сюда.</p>')
REMOTE_SDK = '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
LOCAL_SDK = '<script src="/production-assets/telegram-web-app.js"></script>'


class PreflightError(RuntimeError):
    """A non-secret diagnostic code, never provider error text."""


def emit(**value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def source_text(relative):
    return (SOURCE / 'app/webapp' / relative).read_text()


def login_script():
    script = source_text('js/account.js')
    if script.count(LOGIN_FORM) != 1 or script.count(OLD_LOGIN_BUTTON) != 1:
        raise PreflightError('unexpected_original_login_view')
    # Preserve the existing start/poll cookie, code and bot confirmation flow.
    return script.replace(OLD_LOGIN_BUTTON, '', 1).replace(LOGIN_FORM, LOGIN_ENTRY + LOGIN_FORM, 1)


def index_html():
    html = source_text('index.html')
    if html.count(REMOTE_SDK) != 1:
        raise PreflightError('unexpected_original_sdk_element')
    return html.replace(REMOTE_SDK, LOCAL_SDK, 1)


def voice_script():
    script = source_text('js/voice-training.js')
    if script.count(OLD_ORIGIN + '/app/') != 1:
        raise PreflightError('unexpected_original_voice_hint')
    return script.replace(OLD_ORIGIN + '/app/', ORIGIN + '/app/', 1)


def verify_staged_files():
    for value in (SOURCE, MANIFEST, SDK):
        absolute = Path(os.path.abspath(value))
        if any(path.is_symlink() for path in (*absolute.parents, absolute)):
            raise PreflightError('release_symlink_refused')
    if (not SOURCE.is_dir() or not stat.S_ISREG(MANIFEST.lstat().st_mode)
            or not stat.S_ISREG(SDK.lstat().st_mode)):
        raise PreflightError('release_regular_files_required')
    manifest = json.loads(MANIFEST.read_text())
    files = manifest.get('files') if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        raise PreflightError('unexpected_source_manifest')
    if (manifest.get('baseCommit') != BASE_COMMIT
            or not isinstance(manifest.get('commit'), str)
            or not re.fullmatch(r'[a-f0-9]{40}', manifest['commit'])
            or not isinstance(manifest.get('release'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', manifest['release'])):
        raise PreflightError('unexpected_release_metadata')
    if not REQUIRED_SOURCE <= files.keys():
        raise PreflightError('complete_qr_production_source_required')
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise PreflightError('unsafe_manifest_entry')
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or not relative.parts
                or str(relative) != name or '\\' in name
                or not re.fullmatch(r'[a-f0-9]{64}', expected)):
            raise PreflightError('unsafe_manifest_path')
    actual = {}
    for path in SOURCE.rglob('*'):
        if path.is_symlink():
            raise PreflightError('source_symlink_refused')
        if path.is_file():
            actual[path.relative_to(SOURCE).as_posix()] = digest(path.read_bytes())
        elif not path.is_dir():
            raise PreflightError('source_special_file_refused')
    if actual != files:
        raise PreflightError('source_does_not_match_release_manifest')
    sdk = SDK.read_bytes()
    if len(sdk) != 119731 or digest(sdk) != SDK_SHA256:
        raise PreflightError('unexpected_telegram_sdk')
    # Validate every narrow response overlay before any application import.
    login_script()
    index_html()
    voice_script()
    return manifest


def arm_deadline():
    raw = os.environ.get('PHOTO_BOSS_PRODUCTION_ARM_UNTIL', '').strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError
        value = value.astimezone(timezone.utc)
    except ValueError:
        raise PreflightError('invalid_production_arm_deadline') from None
    if value <= datetime.now(timezone.utc):
        raise PreflightError('production_arm_expired')
    return value


def prepare_runtime():
    if os.environ.get('PHOTO_BOSS_PRODUCTION_ARMED') != '1':
        raise PreflightError('explicit_production_arm_required')
    directory = RUNTIME_ENV.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0
            or stat.S_IMODE(directory.st_mode) & 0o022):
        raise PreflightError('runtime_directory_permissions_unsafe')
    if RUNTIME_ENV.is_symlink() or not RUNTIME_ENV.is_file():
        raise PreflightError('protected_runtime_environment_required')
    info = RUNTIME_ENV.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o027):
        raise PreflightError('runtime_environment_permissions_unsafe')
    if os.environ.get('WEBHOOK_BASE_URL', '').rstrip('/') != ORIGIN:
        raise PreflightError('unexpected_production_origin')
    if os.environ.get('PORT', str(PORT)) != str(PORT):
        raise PreflightError('unexpected_production_port')
    if os.environ.get('PHOTO_BOSS_PRODUCTION_BIND', '127.0.0.1') != '127.0.0.1':
        raise PreflightError('production_must_bind_loopback')
    database = urlsplit(os.environ.get('DATABASE_URL', ''))
    if (database.scheme != 'postgresql+asyncpg' or database.hostname != '127.0.0.1'
            or database.path != '/photoboss_production'
            or database.query or database.fragment or database.port not in (None, 5432)
            or not database.username or not database.password):
        raise PreflightError('fresh_local_production_database_required')
    mode = os.environ.get('PHOTO_BOSS_STANDALONE', '0')
    if mode not in {'0', '1'}:
        raise PreflightError('explicit_application_mode_required')
    if mode == '1':
        secret = os.environ.get('APP_AUTH_SECRET', '')
        qr_key = os.environ.get('QR_SIGNING_KEY', '')
        if (not 32 <= len(secret.encode()) <= 512
                or re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', secret)
                or not re.fullmatch(r'[a-fA-F0-9]{64}', qr_key)
                or len(set(qr_key.lower())) == 1):
            raise PreflightError('independent_application_secrets_required')
        if os.environ.get('PHOTO_BOSS_NOTIFICATION_CHANNEL') != 'app':
            raise PreflightError('standalone_app_notification_channel_required')
        for name in ('BOT_TOKEN', 'EXPECTED_BOT_ID', 'HR_BOT_TOKEN', 'TELEGRAM_API_BASE'):
            os.environ.pop(name, None)
        os.environ.update(MINIAPP_SET_MENU='0', HR_BOT_ENABLED='0')
    else:
        expected = os.environ.get('EXPECTED_BOT_ID', '').strip()
        token = os.environ.get('BOT_TOKEN', '').strip()
        if not expected.isascii() or not expected.isdigit() or token.split(':', 1)[0] != expected:
            raise PreflightError('explicit_matching_bot_identity_required')
    if os.environ.get('MINIAPP_SET_MENU') not in {'0', '1'}:
        raise PreflightError('explicit_menu_policy_required')
    deadline = arm_deadline()
    for name in tuple(os.environ):
        if (name in RESET_COMMANDS or (name.startswith('PHOTO_BOSS_')
                and any(marker in name for marker in ('RESET_REQUEST', 'POINT_SEED', 'TEXT_REQUEST')))):
            os.environ.pop(name, None)
    # A restored configuration must not re-execute Render-only startup actions,
    # load a stray .env, or accidentally start with the read-only trial flag.
    for name in ('RENDER_SERVICE_ID', 'RENDER_EXTERNAL_HOSTNAME', 'RENDER_GIT_COMMIT',
                 'PHOTO_BOSS_TRIAL', 'PHOTO_BOSS_PRODUCTION_ARM_UNTIL'):
        os.environ.pop(name, None)
    os.environ.update(PYTHON_DOTENV_DISABLED='1', PYTHONDONTWRITEBYTECODE='1', PORT=str(PORT))
    sys.dont_write_bytecode = True
    return deadline


def configure_private_logging():
    secrets = set()
    for name, value in os.environ.items():
        if value and (any(word in name for word in ('TOKEN', 'PASSWORD', 'SECRET', 'PRIVATE_KEY', 'API_KEY', 'SIGNING_KEY'))
                      or name in {'DATABASE_URL', 'OPENAI_API_KEY', 'SBER_MERCHANT_LOGIN',
                                  'SBER_QR_CLIENT_ID', 'ADMIN_TELEGRAM_IDS'}):
            if len(value) >= 4:
                secrets.update((value, quote(value, safe=''), quote_plus(value)))
    hidden = tuple(sorted(secrets, key=len, reverse=True))

    class PrivateFormatter(logging.Formatter):
        def format(self, record):
            output = super().format(record)
            for value in hidden:
                output = output.replace(value, '[redacted]')
            return output

    handler = logging.StreamHandler()
    handler.setFormatter(PrivateFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def install_owner_origin(owner_module):
    """Adapt only this module's outgoing owner links; keep source bytes intact."""
    original = owner_module.deliver_owner_copies

    class OwnerOriginBot:
        def __init__(self, bot):
            self.bot = bot

        def __getattr__(self, name):
            return getattr(self.bot, name)

        async def send_message(self, chat_id, text, *args, **kwargs):
            if isinstance(text, str):
                text = text.replace('\n' + OLD_ORIGIN + '/app/#', '\n' + ORIGIN + '/app/#', 1)
            return await self.bot.send_message(chat_id, text, *args, **kwargs)

    async def native_owner_copies(engine, bot):
        return await original(engine, OwnerOriginBot(bot))

    owner_module.deliver_owner_copies = native_owner_copies


def install_native_ui(app, web):
    @web.middleware
    async def native_assets(request, handler):
        if request.method == 'GET':
            if request.path == '/app/':
                return web.Response(text=index_html(), content_type='text/html')
            if request.path == '/app/js/account.js':
                return web.Response(text=login_script(), content_type='text/javascript')
            if request.path == '/app/js/voice-training.js':
                return web.Response(text=voice_script(), content_type='text/javascript')
        return await handler(request)

    async def telegram_sdk(_request):
        return web.FileResponse(SDK, headers={'Content-Type': 'text/javascript; charset=utf-8',
                                             'Cache-Control': 'no-store',
                                             'X-Content-Type-Options': 'nosniff'})

    # Existing MiniApp auth/CSP middleware stays outside this inner responder.
    # No authentication handler, role check or business route is replaced.
    app.middlewares.append(native_assets)
    app.router.add_get('/production-assets/telegram-web-app.js', telegram_sdk)


def install_auth_aliases(app):
    """Retain the original auth guard for two explicit Host/Origin pairs only.

    Password setup/login/logout and browser start/poll all use BrowserLogin's
    ordinary guard. The canonical service origin remains unchanged so permanent
    QR credentials, Telegram prompts, menus and outgoing links use the domain.
    """
    from app.browser_login import BrowserLogin
    from app.miniapp_security import AccessError
    from app.password_login import PasswordLogin

    wanted = {
        '/auth/browser/start': BrowserLogin,
        '/auth/browser/poll': BrowserLogin,
        '/auth/password/login': PasswordLogin,
        '/auth/password/logout': PasswordLogin,
    }
    found = {}
    for route in app.router.routes():
        path = route.resource.canonical
        if route.method == 'POST' and path in wanted:
            if path in found:
                raise PreflightError('duplicate_native_auth_route')
            service = getattr(route.handler, '__self__', None)
            if not isinstance(service, wanted[path]):
                raise PreflightError('unexpected_native_auth_service')
            browser = service.browser if isinstance(service, PasswordLogin) else service
            if type(browser) is not BrowserLogin or browser.origin != ORIGIN:
                raise PreflightError('unexpected_native_auth_origin')
            found[path] = service
    if set(found) != set(wanted):
        raise PreflightError('complete_native_auth_routes_required')
    if (found['/auth/browser/start'] is not found['/auth/browser/poll']
            or found['/auth/password/login'] is not found['/auth/password/logout']):
        raise PreflightError('unexpected_native_auth_instances')
    qr = app.get('staff_qr_login')
    if (qr is None or qr.origin != ORIGIN or qr.allowed_origins != {ORIGIN}):
        raise PreflightError('canonical_staff_qr_service_required')
    original_guard = BrowserLogin.guard

    def native_guard(self, request):
        expected = AUTH_ORIGINS.get(request.host)
        if expected is None or request.headers.get('Origin') != expected:
            raise AccessError('Запрос входа отклонён. Откройте сайт Photo Boss.', 403)
        # Invoke the untouched guard on the matching trusted origin. It still
        # checks the custom session header and rejects cross-site requests.
        trusted = BrowserLogin(self.engine, self.bot, expected)
        original_guard(trusted, request)

    BrowserLogin.guard = native_guard
    qr.allowed_origins = set(AUTH_ORIGINS.values())

    def restore():
        BrowserLogin.guard = original_guard
        qr.allowed_origins = {ORIGIN}

    return restore


def install_arm_expiry(app, deadline):
    if deadline is None:
        return

    async def check_before_startup(_app):
        if datetime.now(timezone.utc) >= deadline:
            raise PreflightError('production_arm_expired')

    async def expiry_watchdog(_app):
        async def stop_at_deadline():
            while True:
                remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    logging.getLogger(__name__).warning('Production arm expired; graceful stop requested')
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                await asyncio.sleep(min(remaining, 15))
        task = asyncio.create_task(stop_at_deadline())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.insert(0, check_before_startup)
    app.cleanup_ctx.insert(0, expiry_watchdog)


def launch():
    # This gate intentionally precedes every import of app.*.
    deadline = prepare_runtime()
    manifest = verify_staged_files()
    configure_private_logging()
    sys.path.insert(0, str(SOURCE))
    if os.environ.get('PHOTO_BOSS_STANDALONE') == '1':
        from aiohttp import web
        from app.standalone_main import create_app
        application = create_app(start_workers=True)
        restore_aliases = install_auth_aliases(application)
        install_arm_expiry(application, deadline)
        emit(starting=True, mode='standalone', sourceFiles=len(manifest['files']),
             sourceManifestSha256=digest(MANIFEST.read_bytes()), origin=ORIGIN,
             bind='127.0.0.1', port=PORT, telegramWebhookAndWorkers='disabled',
             nativeBusinessWorkers='armed', qrLoginEnabled=True,
             sourceMatchesReleaseManifest=True, boundedArm=deadline is not None)
        try:
            return web.run_app(application, host='127.0.0.1', port=PORT,
                               print=None, access_log=None)
        finally:
            restore_aliases()
    from app import owner_telegram, render_main

    install_owner_origin(owner_telegram)
    original_run_app = render_main.web.run_app
    alias_restores = []

    def native_run_app(app, **options):
        # Recheck the lease before the original application's startup callbacks.
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            raise PreflightError('production_arm_expired')
        alias_restores.append(install_auth_aliases(app))
        install_native_ui(app, render_main.web)
        install_arm_expiry(app, deadline)
        options.update(host='127.0.0.1', port=PORT)
        options['access_log'] = None
        emit(starting=True, mode='full-production', sourceFiles=len(manifest['files']),
             sourceManifestSha256=digest(MANIFEST.read_bytes()),
             origin=ORIGIN, bind='127.0.0.1', port=PORT,
             telegramWebhookAndWorkers='armed', sourceMatchesReleaseManifest=True,
             qrLoginEnabled=True, trustedAuthOrigins=sorted(AUTH_ORIGINS.values()),
             boundedArm=deadline is not None)
        return original_run_app(app, **options)

    render_main.web.run_app = native_run_app
    try:
        render_main.main()
    finally:
        render_main.web.run_app = original_run_app
        for restore in reversed(alias_restores):
            restore()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check-only', action='store_true')
    mode.add_argument('--arm-production', action='store_true')
    args = parser.parse_args()
    if args.check_only:
        manifest = verify_staged_files()
        emit(prepared=True, sourceFiles=len(manifest['files']),
             sourceManifestSha256=digest(MANIFEST.read_bytes()),
             completeProductionEntry=True, qrSourcePresent=True,
             sdkVerified=True, runtimeStarted=False, providerWrites=False)
    else:
        launch()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit(started=False, error=str(error) if isinstance(error, PreflightError)
             else type(error).__name__)
        raise SystemExit(1) from None
