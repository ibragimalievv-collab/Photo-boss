"""Native application context: local identity and explicit Telegram refusal."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger(__name__)


class TelegramDisabledError(RuntimeError):
    """A legacy Telegram call must never masquerade as delivered work."""


class LocalSession:
    async def close(self):
        pass


class LocalAppContext:
    """Minimal compatibility for shared web handlers, never a Telegram Bot.

    token is the independent application secret for legacy rate-limit/service
    identities. QR signatures use their separately preserved derived key.
    """
    id = 0

    def __init__(self, configuration):
        if not getattr(configuration, 'standalone', False):
            raise ValueError('Standalone application context requires explicit mode')
        configuration.validate()
        self._secret = configuration.app_auth_secret
        self.blocked_telegram_calls = 0
        self.session = LocalSession()
        os.environ['PHOTO_BOSS_NOTIFICATION_CHANNEL'] = 'app'

    @property
    def token(self):
        return self._secret

    def __repr__(self):
        return '<LocalAppContext standalone=True telegram=False>'

    async def me(self):
        return SimpleNamespace(id=self.id, username=None, first_name='Photo Boss',
                               is_bot=False, standalone=True)

    async def __call__(self, *args, **kwargs):
        self.blocked_telegram_calls += 1
        raise TelegramDisabledError('Telegram отключён в самостоятельном приложении.')

    def __getattr__(self, name):
        if name.startswith(('send_', 'get_', 'set_', 'delete_', 'edit_', 'answer_', 'download')):
            async def disabled(*args, **kwargs):
                self.blocked_telegram_calls += 1
                raise TelegramDisabledError('Telegram отключён в самостоятельном приложении.')
            return disabled
        raise AttributeError(name)


def worker_map(configuration, *, enabled):
    from .services.ai_provider import ai_configured
    always = ('operations', 'full_backup', 'owner_app_notices', 'booking_reminders',
              'push', 'storage_originals_and_edits', 'storage_recovery', 'chat_retention',
              'disk_audit', 'storage_inventory', 'bank_reconciliation', 'guest_sms',
              'attendance_fines', 'realtime_expiry')
    result = {name: 'scheduled' if enabled else 'paused' for name in always}
    for name in ('academy_review', 'development_review'):
        result[name] = ('scheduled' if ai_configured(configuration, vision=True) else 'not_configured') if enabled else 'paused'
    result['avito_hr'] = ('scheduled' if all(os.getenv(k) for k in ('AVITO_CLIENT_ID', 'AVITO_CLIENT_SECRET'))
                          else 'not_configured') if enabled else 'paused'
    result['telegram_delivery'] = 'replaced_by_app_notifications'
    result['assignment_telegram_delivery'] = 'replaced_by_durable_app_and_push'
    result['telegram_backup'] = 'replaced_by_private_full_backup'
    result['telegram_guest_handoff'] = 'replaced_by_guest_gallery_link'
    result['telegram_hr_bot'] = 'replaced_by_staff_qr_invitation'
    from .services.automatic_receipts import enabled as receipt_auto_enabled
    result['receipt_evidence'] = ('scheduled' if enabled else 'paused') if receipt_auto_enabled() else 'not_enabled'
    return result


async def owner_failure_notice(engine, worker, code):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from .models import User, UserRole
    from .push_notifications import queue_notice
    async with AsyncSession(engine) as session:
        owners = list(await session.scalars(select(User.id).where(User.active.is_(True),
            User.id.in_(select(UserRole.user_id).where(UserRole.role == 'OWNER')))))
        day = datetime.now(UTC).date().isoformat()
        for uid in owners:
            # operational_alert is booking-scoped in both feed and push rules.
            # A system/backup failure has no booking and is owner-only status.
            await queue_notice(session, uid, kind='service_status',
                event_key=f'standalone-worker:{worker}:{day}:{code}',
                message=('Требует проверки: резервная копия.' if worker == 'full_backup'
                         else 'Требует проверки: фоновая задача приложения.')
                        + ' Выполнение не подтверждено; данные сохранены. Код: ' + code,
                url='/app/#more')
        await session.commit()


async def periodic(engine, name, operation, *, interval):
    while True:
        try:
            await operation()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- errors may contain URLs or credentials
            # Exception bodies may contain storage URLs or database credentials.
            from .standalone_backup import BackupError
            code = str(error) if isinstance(error, BackupError) else type(error).__name__
            logger.warning('Standalone task unavailable name=%s code=%s', name, code)
            try:
                await owner_failure_notice(engine, name, code)
            except Exception:  # noqa: BLE001 -- notice delivery is best effort
                logger.warning('Standalone task notice deferred name=%s', name)
        await asyncio.sleep(interval)


async def ensure_owner_notice_checkpoint(engine):
    from sqlalchemy.ext.asyncio import AsyncSession

    from .models import Setting, utc_now
    from .owner_telegram import START_KEY
    # This checkpoint starts only current app notices, never old Telegram jobs.
    async with AsyncSession(engine) as session:
        if await session.get(Setting, START_KEY) is None:
            session.add(Setting(key=START_KEY, value=utc_now().isoformat()))
            await session.commit()


async def owner_notice_cycle(engine, storage):
    from .owner_telegram import queue_connection_state, queue_owner_work_events
    await ensure_owner_notice_checkpoint(engine)
    await queue_owner_work_events(engine)
    await queue_connection_state(engine, storage)


async def default_worker_factories(app, api, engine, configuration, context):
    from sqlalchemy.ext.asyncio import AsyncSession

    from .bank_payments import payment_loop
    from .disk_folder_audit import disk_audit_loop
    from .operations_guard import operation_lease
    from .push_notifications import push_loop
    from .services.ai_provider import ai_configured
    from .services.booking_reminders import booking_reminder_loop
    from .services.guest_sms import guest_sms_loop
    from .services.operations import run_operations_once
    from .services.photo_storage import storage_loop
    from .standalone_backup import StandaloneBackups
    from .storage_inventory import log_recent_inventory
    from .work_chat import cleanup_loop
    from .yandex_disk import storage_recovery_loop
    storage = app['yandex_disk']
    from .bank_payments import configured as acquiring_configured
    from .push_notifications import configured as push_configured
    from .sber_qr_client import qr_client_issues as qr_issues
    from .sber_qr_config import SberQRConfig
    state = app['worker_map']
    if not push_configured():
        state['push'] = 'idle_unconfigured'
    try:
        bank_ready = acquiring_configured() or not qr_issues(SberQRConfig.from_env(), for_order=True)
    except (ValueError, OSError):
        bank_ready = False
    if not bank_ready:
        state['bank_reconciliation'] = 'idle_unconfigured'
    if not getattr(app.get('guest_sms'), 'available', False):
        state['guest_sms'] = 'idle_unconfigured'
    if not storage.public_status().get('configured'):
        for name in ('storage_originals_and_edits', 'storage_recovery', 'disk_audit', 'storage_inventory'):
            state[name] = 'idle_unconfigured'
    directory = Path(os.getenv('PHOTO_BOSS_STANDALONE_BACKUP_DIR',
                               '/var/lib/photoboss-production/standalone-backups'))
    backups = StandaloneBackups(configuration, storage, directory)
    app['standalone_backups'] = backups
    await ensure_owner_notice_checkpoint(engine)

    async def operations():
        async with operation_lease(engine), AsyncSession(engine) as session:
            await run_operations_once(session, context)

    factories = {
        'operations': lambda: periodic(engine, 'operations', operations, interval=300),
        'full_backup': lambda: periodic(engine, 'full_backup', backups.run_once, interval=300),
        'owner_app_notices': lambda: periodic(engine, 'owner_app_notices',
            lambda: owner_notice_cycle(engine, storage), interval=5),
        'booking_reminders': lambda: booking_reminder_loop(engine),
        'push': lambda: push_loop(engine),
        'storage_originals_and_edits': lambda: storage_loop(context, storage),
        'storage_recovery': lambda: storage_recovery_loop(storage),
        'chat_retention': lambda: cleanup_loop(engine, storage),
        'disk_audit': lambda: disk_audit_loop(engine),
        'storage_inventory': lambda: log_recent_inventory(engine, storage),
        'bank_reconciliation': lambda: payment_loop(engine),
        'guest_sms': lambda: guest_sms_loop(engine, service=app.get('guest_sms')),
    }
    from .services.automatic_receipts import enabled as receipt_auto_enabled
    from .services.automatic_receipts import worker as receipt_worker
    if receipt_auto_enabled():
        factories['receipt_evidence'] = lambda: receipt_worker(engine, context)
    from .attendance import review_required
    if review_required():
        from .attendance_fines import fine_loop, initialize, worker_api
        fine_api = worker_api(engine, configuration.training_timezone)
        await initialize(fine_api, datetime.now(UTC))
        factories['attendance_fines'] = lambda: fine_loop(fine_api)
    else:
        app['worker_map']['attendance_fines'] = 'not_enabled'
    if ai_configured(configuration, vision=True):
        from .development import development_loop
        factories['academy_review'] = lambda: app['academy_practice'].worker(app)
        factories['development_review'] = lambda: development_loop(engine, context)
    avito = app.get('standalone_avito')
    if avito is not None:
        factories['avito_hr'] = avito.run
    return factories


async def start_worker_tasks(app, api, engine, configuration, context, *, factories=None):
    if os.getenv('PHOTO_BOSS_NOTIFICATION_CHANNEL') != 'app':
        raise RuntimeError('standalone_app_notification_channel_required')
    if factories is None:
        factories = await default_worker_factories(app, api, engine, configuration, context)
    app['standalone_tasks'] = {}
    def finished(name, task):
        if task.cancelled():
            return
        if name == 'avito_hr':
            app['standalone_avito'].ready = False
        if name == 'storage_inventory' and task.exception() is None:
            app['worker_map'][name] = 'completed'
        else:
            app['worker_map'][name] = 'failed'
            logger.error('Standalone task stopped unexpectedly name=%s', name)
    for name, factory in factories.items():
        task = asyncio.create_task(factory(), name='standalone:' + name)
        if app['worker_map'].get(name) == 'scheduled':
            app['worker_map'][name] = 'running'
        task.add_done_callback(lambda done, name=name: finished(name, done))
        app['standalone_tasks'][name] = task


async def stop_worker_tasks(app):
    tasks = list(app.get('standalone_tasks', {}).values())
    app['standalone_tasks'] = {}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
