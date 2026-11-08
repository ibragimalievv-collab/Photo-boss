"""Bounded full PostgreSQL backups: private local copy and encrypted private Disk."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
from zoneinfo import ZoneInfo

MAX_DUMP_BYTES = 64 * 1024 * 1024
MAGIC = b'PHOTO_BOSS_AESGCM_BACKUP_V1\n'
AAD = b'photo-boss:standalone-full-backup:v1'
DISK_ROOT = 'app:/PhotoBoss/system/standalone-backups'


class BackupError(RuntimeError):
    """Only fixed non-secret codes leave a worker."""


def require(value, code):
    if not value:
        raise BackupError(code)


def private_directory(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, 'unsafe_backup_directory')
    if not path.exists():
        parent = path.parent.stat()
        require(stat.S_ISDIR(parent.st_mode) and parent.st_uid in {0, os.geteuid()}
                and not stat.S_IMODE(parent.st_mode) & 0o022, 'unsafe_backup_parent')
        path.mkdir(mode=0o700)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, 'private_backup_directory_required')
    return path


def private_read(path, maximum=MAX_DUMP_BYTES + 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= maximum,
                'private_backup_file_required')
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, 'backup_size_limit')
    return raw


def private_write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def save_manifest(path, value):
    fd, name = tempfile.mkstemp(prefix='.backup-state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(value, sort_keys=True, allow_nan=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)  # Only this just-created temporary manifest.


@contextmanager
def backup_lock(directory):
    fd = os.open(directory / '.daily-backup.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600, 'unsafe_backup_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError('backup_already_running') from None
        yield
    finally:
        os.close(fd)


def encryption_key(secret):
    require(isinstance(secret, str) and len(secret.encode()) >= 32, 'backup_key_unavailable')
    return hmac.new(secret.encode(), b'photo-boss:standalone-backup-key:v1', hashlib.sha256).digest()


def encrypt_dump(raw, secret):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    require(0 < len(raw) <= MAX_DUMP_BYTES and raw.startswith(b'PGDMP'), 'invalid_full_backup')
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + AESGCM(encryption_key(secret)).encrypt(nonce, raw, AAD)


async def postgres_dump(configuration, output):
    """Use the app's local DB role, a private temporary PGPASSFILE and bounded pipes."""
    from sqlalchemy.engine import make_url

    database = make_url(configuration.database_url)
    require(database.drivername == 'postgresql+asyncpg'
            and database.host in {'127.0.0.1', 'localhost'}
            and database.username and database.password and database.database,
            'local_backup_database_required')
    port = database.port or 5432
    fields = [database.host, str(port), database.database, database.username, database.password]
    require(all(isinstance(v, str) and '\n' not in v and '\r' not in v for v in fields),
            'invalid_backup_database_credentials')
    password_path = output.parent / ('.pgpass-' + secrets.token_hex(16))
    pgpass = ':'.join(v.replace('\\', '\\\\').replace(':', '\\:') for v in fields) + '\n'
    private_write(password_path, pgpass.encode())
    environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
        'PGHOST': database.host, 'PGPORT': str(port), 'PGDATABASE': database.database,
        'PGUSER': database.username, 'PGPASSFILE': str(password_path), 'PGCONNECT_TIMEOUT': '10'}
    process = None
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            async with asyncio.timeout(180):
                process = await asyncio.create_subprocess_exec(
                    '/usr/bin/pg_dump', '--format=custom', '--no-owner', '--no-acl',
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=environment)

                async def drain_stderr():
                    # Discard diagnostics: even errors must not expose a credential.
                    while await process.stderr.read(65536):
                        pass

                errors = asyncio.create_task(drain_stderr())
                try:
                    size = 0
                    while chunk := await process.stdout.read(65536):
                        size += len(chunk)
                        require(size <= MAX_DUMP_BYTES, 'backup_size_limit')
                        stream.write(chunk)
                    require(await process.wait() == 0 and size > 0, 'database_dump_failed')
                    await errors
                    stream.flush()
                    os.fsync(stream.fileno())
                finally:
                    if not errors.done():
                        errors.cancel()
                        await asyncio.gather(errors, return_exceptions=True)
        require(private_read(output).startswith(b'PGDMP'), 'invalid_full_backup')
        async with asyncio.timeout(60):
            process = await asyncio.create_subprocess_exec(
                '/usr/bin/pg_restore', '--list', str(output), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin'})
            require(await process.wait() == 0, 'backup_catalog_invalid')
    except (OSError, TimeoutError):
        raise BackupError('database_backup_unavailable') from None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        password_path.unlink(missing_ok=True)  # This one-use credential file only.


class StandaloneBackups:
    def __init__(self, configuration, storage, directory, *, dump=postgres_dump):
        self.config, self.storage = configuration, storage
        self.directory, self.dump = Path(directory), dump
        self.state = {'localVerified': False, 'diskVerified': False, 'checkedAt': None,
                      'errorCode': None, 'fullDatabase': True, 'diskEncrypted': True,
                      'diskVerifiedAt': None, 'restoreRehearsed': False}

    def public_status(self):
        return dict(self.state)

    async def run_once(self, now=None):
        try:
            return await self._run_once(now)
        except BackupError as error:
            self.state['errorCode'] = str(error)
            raise
        except Exception:
            self.state['errorCode'] = 'standalone_backup_unavailable'
            raise BackupError('standalone_backup_unavailable') from None

    async def _run_once(self, now=None):
        from .yandex_disk import SYSTEM, validate_saved_digest

        now = now or datetime.now(UTC)
        local = now.astimezone(ZoneInfo(self.config.training_timezone))
        if local.hour < 3:
            return self.public_status()
        directory = private_directory(self.directory)
        day = local.date().isoformat()
        self.state.update(checkedAt=now.isoformat(), errorCode=None, localVerified=False,
                          diskVerified=False, diskVerifiedAt=None)
        key_id = hmac.new(encryption_key(self.config.app_auth_secret), b'backup-key-identity',
                          hashlib.sha256).hexdigest()
        with backup_lock(directory):
            manifest_path = directory / (day + '.json')
            if manifest_path.exists():
                saved = json.loads(private_read(manifest_path, 32768))
                require(saved.get('day') == day and saved.get('schema') == 1, 'invalid_backup_manifest')
            else:
                # The durable attempt is allocated BEFORE pg_dump. A crash or
                # manifest failure cannot produce another 128 MiB every cycle.
                # Keep uncertain artifacts for review instead of deleting them.
                attempt_path = directory / (day + '.attempt.json')
                if attempt_path.exists():
                    attempt = json.loads(private_read(attempt_path, 32768))
                    require(attempt.get('schema') == 1 and attempt.get('day') == day
                            and attempt.get('keyId') == key_id, 'invalid_backup_attempt')
                else:
                    name = day + '-' + secrets.token_hex(12)
                    attempt = {'schema': 1, 'day': day, 'keyId': key_id,
                        'dump': name + '.dump', 'cipher': name + '.dump.aesgcm'}
                    private_write(attempt_path, json.dumps(attempt, sort_keys=True).encode())
                    descriptor = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                require(all(isinstance(attempt.get(k), str) and Path(attempt[k]).name == attempt[k]
                            for k in ('dump', 'cipher')), 'invalid_backup_attempt_path')
                source = directory / attempt['dump']
                cipher = directory / attempt['cipher']
                require(not source.exists() and not cipher.exists(),
                        'backup_incomplete_attempt_requires_review')
                await self.dump(self.config, source)
                raw = private_read(source)
                require(raw.startswith(b'PGDMP'), 'invalid_full_backup')
                self.state.update(localVerified=True, diskVerified=False)
                encrypted = encrypt_dump(raw, self.config.app_auth_secret)
                private_write(cipher, encrypted)
                saved = {'schema': 1, 'day': day, 'createdAt': now.isoformat(),
                    'keyId': key_id,
                    'dump': source.name, 'dumpSHA256': hashlib.sha256(raw).hexdigest(),
                    'cipher': cipher.name, 'cipherSHA256': hashlib.sha256(encrypted).hexdigest(),
                    'cipherMD5': hashlib.md5(encrypted, usedforsecurity=False).hexdigest(),
                    'cipherBytes': len(encrypted), 'diskVerified': False, 'restoreRehearsed': False}
                save_manifest(manifest_path, saved)
            for field in ('dump', 'cipher'):
                require(isinstance(saved.get(field), str) and Path(saved[field]).name == saved[field],
                        'invalid_backup_manifest_path')
            source, cipher = directory / saved['dump'], directory / saved['cipher']
            require(hashlib.sha256(private_read(source)).hexdigest() == saved['dumpSHA256'],
                    'local_backup_changed')
            self.state['localVerified'] = True
            require(saved.get('keyId') == key_id, 'backup_key_changed_preserve_existing')
            encrypted = private_read(cipher)
            require(hashlib.sha256(encrypted).hexdigest() == saved['cipherSHA256']
                    and len(encrypted) == saved['cipherBytes'] and encrypted.startswith(MAGIC),
                    'encrypted_backup_changed')
            self.state.update(localVerified=True, diskVerified=bool(saved['diskVerified']),
                              diskVerifiedAt=saved.get('diskVerifiedAt'))
            if saved['diskVerified']:
                return self.public_status()
            try:
                require(self.storage is not None and self.storage.public_status().get('configured'),
                        'backup_disk_not_configured')
                for path in ('app:/PhotoBoss', SYSTEM, DISK_ROOT):
                    await self.storage.ensure_dir(path)
                with cipher.open('rb') as stream:
                    metadata = await self.storage.upload_file(DISK_ROOT + '/' + cipher.name, stream,
                        size=len(encrypted), sha256=saved['cipherSHA256'], md5=saved['cipherMD5'])
                validate_saved_digest(metadata, len(encrypted), saved['cipherSHA256'], saved['cipherMD5'])
                require(not metadata.get('public_url') and not metadata.get('public_key'),
                        'backup_disk_not_private')
                saved['diskVerified'] = True
                saved['diskVerifiedAt'] = datetime.now(UTC).isoformat()
                save_manifest(manifest_path, saved)
                self.state.update(diskVerified=True, errorCode=None,
                                  diskVerifiedAt=saved['diskVerifiedAt'])
            except Exception as error:
                code = str(error) if isinstance(error, BackupError) else 'backup_disk_unverified'
                self.state.update(diskVerified=False, errorCode=code)
                raise BackupError(code) from None
        return self.public_status()
