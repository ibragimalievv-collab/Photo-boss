"""Persistent Telegram -> Yandex.Disk sync for shooting photos."""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from datetime import UTC, datetime, timedelta

import aiohttp
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..db import Session
from ..models import PhotoStorage
from ..yandex_disk import ROOT, YandexDisk, YandexDiskError, validate_saved

logger = logging.getLogger(__name__)

MAX_TELEGRAM_IMAGE_BYTES = 20 * 1024 * 1024
MAX_ATTEMPTS = 5
STALE_UPLOAD = timedelta(minutes=10)


class LimitedBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_TELEGRAM_IMAGE_BYTES:
            raise ValueError("Фото больше 20 МБ; отправьте его как обычное фото или уменьшите файл.")
        return super().write(data)


def image_format(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise ValueError("Поддерживаются JPEG, PNG и WebP.")


async def ensure_photo_storage(
    session,
    *,
    photo_id: int,
    shooting_id: int,
    file_id: str,
    file_unique_id: str,
    source_kind: str,
):
    existing = await session.scalar(
        select(PhotoStorage).where(
            PhotoStorage.shooting_id == shooting_id,
            PhotoStorage.telegram_unique_id == file_unique_id,
        )
    )
    if existing is not None:
        return existing, False
    row = PhotoStorage(
        photo_id=photo_id,
        shooting_id=shooting_id,
        telegram_file_id=file_id,
        telegram_unique_id=file_unique_id,
        source_kind=source_kind,
        status="PENDING",
        attempts=0,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        found = await session.scalar(
            select(PhotoStorage).where(
                PhotoStorage.shooting_id == shooting_id,
                PhotoStorage.telegram_unique_id == file_unique_id,
            )
        )
        return found, False
    return row, True


async def _claim_job():
    now = datetime.now(UTC).replace(tzinfo=None)
    stale = now - STALE_UPLOAD
    async with Session() as session:
        await session.execute(update(PhotoStorage).where(
            PhotoStorage.status == 'UPLOADING', PhotoStorage.started_at < stale,
            PhotoStorage.attempts >= MAX_ATTEMPTS,
        ).values(status='FAILED', last_error='Последняя попытка прервана. Повторите загрузку.', updated_at=now))
        row = await session.scalar(
            select(PhotoStorage)
            .where(
                PhotoStorage.attempts < MAX_ATTEMPTS,
                or_(
                    PhotoStorage.status == "PENDING",
                    and_(PhotoStorage.status == "FAILED", PhotoStorage.updated_at < now - timedelta(seconds=60)),
                    and_(
                        PhotoStorage.status == "UPLOADING",
                        PhotoStorage.started_at.is_not(None),
                        PhotoStorage.started_at < stale,
                    ),
                ),
            )
            .order_by(PhotoStorage.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            await session.commit()
            return None
        row.status = "UPLOADING"
        row.attempts += 1
        row.started_at = now
        row.updated_at = now
        row.last_error = None
        result = {
            "id": row.id,
            "photo_id": row.photo_id,
            "shooting_id": row.shooting_id,
            "file_id": row.telegram_file_id,
            "attempt": row.attempts,
            "started_at": now,
        }
        await session.commit()
        return result


async def _download(bot, file_id: str) -> bytes:
    from .media_files import load_private_media
    data = await load_private_media(bot, file_id, max_bytes=MAX_TELEGRAM_IMAGE_BYTES, timeout=40)
    if len(data) < 100:
        raise ValueError("Telegram вернул пустой или повреждённый файл.")
    return data


async def _existing_hash(shooting_id: int, digest: str, current_id: int):
    async with Session() as session:
        return await session.scalar(
            select(PhotoStorage)
            .where(
                PhotoStorage.shooting_id == shooting_id,
                PhotoStorage.sha256 == digest,
                PhotoStorage.status == "STORED",
                PhotoStorage.id != current_id,
            )
            .order_by(PhotoStorage.id)
            .limit(1)
        )


async def _finish_success(job_id: int, *, path: str, digest: str, size: int, attempt: int, started_at):
    now = datetime.now(UTC).replace(tzinfo=None)
    async with Session() as session:
        row = await session.get(PhotoStorage, job_id, with_for_update=True)
        if row is None or row.status != "UPLOADING" or row.attempts != attempt or row.started_at != started_at:
            return
        row.status = "STORED"
        row.disk_path = path
        row.sha256 = digest
        row.byte_size = size
        row.stored_at = now
        row.updated_at = now
        row.last_error = None
        await session.commit()


async def _finish_failure(job_id: int, exc: Exception, *, attempt: int, started_at):
    now = datetime.now(UTC).replace(tzinfo=None)
    safe = "Хранилище заполнено. Освободите место и повторите." if "507" in str(exc) else "Не удалось подтвердить сохранение. Повторите загрузку."
    async with Session() as session:
        row = await session.get(PhotoStorage, job_id, with_for_update=True)
        if row is None or row.status != "UPLOADING" or row.attempts != attempt or row.started_at != started_at:
            return
        row.status = "FAILED"
        row.last_error = safe
        row.updated_at = now
        await session.commit()
    logger.warning("Photo storage job %s failed (%s)", job_id, type(exc).__name__)


async def sync_one(bot, storage: YandexDisk) -> bool:
    job = await _claim_job()
    if job is None:
        return False
    try:
        if not storage.state.get("connected"):
            status = await storage.verify(write_test=False)
            if not status.get("connected"):
                raise RuntimeError("Yandex.Disk is unavailable")
        data = await _download(bot, job["file_id"])
        digest = hashlib.sha256(data).hexdigest()
        ext, content_type = image_format(data)
        duplicate = await _existing_hash(job["shooting_id"], digest, job["id"])
        if duplicate is not None and duplicate.disk_path:
            path = duplicate.disk_path
            try:
                validate_saved(await storage.metadata(path), data)
            except YandexDiskError:
                # Repair a missing/corrupt copy instead of trusting an old STORED row.
                await storage.upload_bytes(path, data, content_type=content_type)
        else:
            base = ROOT + "/shootings"
            folder = base + f"/{job['shooting_id']}"
            await storage.ensure_dir(base)
            await storage.ensure_dir(folder)
            path = folder + f"/{job['photo_id']}_{digest[:12]}.{ext}"
            await storage.upload_bytes(path, data, content_type=content_type)
        await _finish_success(job["id"], path=path, digest=digest, size=len(data), attempt=job["attempt"], started_at=job["started_at"])
        logger.info(
            "Photo stored on Yandex.Disk: photo=%s shooting=%s bytes=%s",
            job["photo_id"], job["shooting_id"], len(data),
        )
    except (
        TelegramAPIError,
        aiohttp.ClientError,
        OSError,
        ValueError,
        RuntimeError,
        asyncio.TimeoutError,
    ) as exc:
        await _finish_failure(job["id"], exc, attempt=job["attempt"], started_at=job["started_at"])
    return True


async def storage_loop(bot, storage: YandexDisk, *, interval=15, batch=3):
    while True:
        try:
            processed = 0
            for _ in range(batch):
                if not await sync_one(bot, storage):
                    break
                processed += 1
            from ..photo_edits import process_one
            await process_one(storage)
            if processed:
                logger.info("Yandex.Disk photo sync cycle: processed=%s", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Yandex.Disk photo sync loop failed")
        await asyncio.sleep(interval)


async def storage_summary(session, shooting_id: int) -> dict:
    rows = (
        await session.scalars(
            select(PhotoStorage).where(PhotoStorage.shooting_id == shooting_id)
        )
    ).all()
    return {
        "total": len(rows),
        "stored": sum(1 for row in rows if row.status == "STORED"),
        "pending": sum(1 for row in rows if row.status in {"PENDING", "UPLOADING"}),
        "failed": sum(1 for row in rows if row.status == "FAILED"),
    }


class StorageConfirmationPending(ValueError):
    """Retry only after storage catches up; never finalize money on this result."""


async def require_confirmed_originals(session, shooting_id):
    """Every original needs its own validated storage receipt, not a zero-error count.

    The caller must hold the Shooting lock while closing an upload. STORED is
    written by sync_one only after Yandex size/hash verification. Keep this check
    local to avoid 1000 network requests inside a financial transaction.
    """
    import re

    from ..models import Photo

    pairs = (await session.execute(select(Photo.id, PhotoStorage).outerjoin(
        PhotoStorage, and_(PhotoStorage.photo_id == Photo.id,
                           PhotoStorage.shooting_id == Photo.shooting_id)
    ).where(Photo.shooting_id == shooting_id))).all()
    missing = sum(row is None for _, row in pairs)
    pending = sum(row is not None and row.status in {'PENDING', 'UPLOADING'} for _, row in pairs)
    failed = sum(row is not None and row.status == 'FAILED' for _, row in pairs)
    invalid = 0
    hashes = []
    prefix = ROOT + f'/shootings/{shooting_id}/'
    for _, row in pairs:
        if row is None or row.status != 'STORED':
            continue
        if (not row.disk_path or not row.disk_path.startswith(prefix)
                or not row.stored_at or not row.byte_size or row.byte_size < 100
                or not re.fullmatch(r'[0-9a-f]{64}', row.sha256 or '')):
            invalid += 1
        else:
            hashes.append(row.sha256)
    if not pairs or missing or pending or failed or invalid or len(hashes) != len(pairs):
        raise StorageConfirmationPending(
            f'Полная загрузка не завершена: подтверждено {len(hashes)} из {len(pairs)}. '
            f'Без записи хранения: {missing}; в очереди: {pending}; ошибки: {failed}; '
            f'без проверки файла: {invalid}. Дождитесь сохранения или повторите восстановление на Диск.'
        )
    if len(hashes) != len(set(hashes)):
        raise ValueError('В архиве есть точные копии одного кадра. Проверьте дубли до завершения загрузки; ставка не изменена.')
    return len(pairs)


async def repair_storage_queue(session, shooting_id):
    """Rebuild missing jobs from known Photo file references. No originals are deleted.

    Called only after a scoped write authorization and the Shooting row lock.
    Recovery identifiers are local job markers, not invented Telegram identities.
    """
    import re

    from ..models import Photo

    pairs = (await session.execute(select(Photo, PhotoStorage).outerjoin(
        PhotoStorage, PhotoStorage.photo_id == Photo.id
    ).where(Photo.shooting_id == shooting_id))).all()
    enqueued = retried = 0
    prefix = ROOT + f'/shootings/{shooting_id}/'
    for photo, row in pairs:
        if row is None:
            session.add(PhotoStorage(photo_id=photo.id, shooting_id=shooting_id,
                telegram_file_id=photo.file_id, telegram_unique_id=f'recovery-photo:{photo.id}',
                source_kind='DOCUMENT', status='PENDING', attempts=0))
            enqueued += 1
            continue
        valid = (row.status == 'STORED' and row.shooting_id == shooting_id
                 and row.disk_path and row.disk_path.startswith(prefix)
                 and row.stored_at and row.byte_size and row.byte_size >= 100
                 and re.fullmatch(r'[0-9a-f]{64}', row.sha256 or ''))
        if row.shooting_id != shooting_id or row.status == 'FAILED' or (row.status == 'STORED' and not valid):
            row.status, row.attempts, row.last_error = 'PENDING', 0, None
            row.started_at = None
            row.shooting_id = shooting_id
            row.telegram_file_id = photo.file_id
            retried += 1
    await session.flush()
    return {'retried': retried, 'enqueued': enqueued}
