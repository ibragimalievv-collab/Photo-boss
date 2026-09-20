"""Persistent Telegram -> Yandex.Disk sync for shooting photos."""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from datetime import UTC, datetime, timedelta

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from ..db import Session
from ..models import Photo, PhotoStorage
from ..yandex_disk import ROOT, YandexDisk

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
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        async with Session() as recovery:
            found = await recovery.scalar(
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
        row = await session.scalar(
            select(PhotoStorage)
            .where(
                PhotoStorage.attempts < MAX_ATTEMPTS,
                or_(
                    PhotoStorage.status == "PENDING",
                    PhotoStorage.status == "FAILED",
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
        }
        await session.commit()
        return result


async def _download(bot, file_id: str) -> bytes:
    buffer = LimitedBuffer()
    await bot.download(file_id, destination=buffer, timeout=40)
    data = buffer.getvalue()
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


async def _finish_success(job_id: int, *, path: str, digest: str, size: int):
    now = datetime.now(UTC).replace(tzinfo=None)
    async with Session() as session:
        row = await session.get(PhotoStorage, job_id, with_for_update=True)
        if row is None:
            return
        row.status = "STORED"
        row.disk_path = path
        row.sha256 = digest
        row.byte_size = size
        row.stored_at = now
        row.updated_at = now
        row.last_error = None
        await session.commit()


async def _finish_failure(job_id: int, exc: Exception):
    now = datetime.now(UTC).replace(tzinfo=None)
    safe = f"{type(exc).__name__}: {str(exc)[:180]}"
    async with Session() as session:
        row = await session.get(PhotoStorage, job_id, with_for_update=True)
        if row is None:
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
        else:
            base = ROOT + "/shootings"
            folder = base + f"/{job['shooting_id']}"
            await storage.ensure_dir(base)
            await storage.ensure_dir(folder)
            path = folder + f"/{job['photo_id']}_{digest[:12]}.{ext}"
            await storage.upload_bytes(path, data, content_type=content_type)
        await _finish_success(job["id"], path=path, digest=digest, size=len(data))
        logger.info(
            "Photo stored on Yandex.Disk: photo=%s shooting=%s bytes=%s",
            job["photo_id"], job["shooting_id"], len(data),
        )
    except (TelegramAPIError, OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
        await _finish_failure(job["id"], exc)
    return True


async def storage_loop(bot, storage: YandexDisk, *, interval=15, batch=3):
    while True:
        try:
            processed = 0
            for _ in range(batch):
                if not await sync_one(bot, storage):
                    break
                processed += 1
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
