"""Read private media saved by either Telegram or the web uploader.

An app-folder path is a server-side reference, never a public download URL.
Callers must authorize the business object before loading its file reference.
"""
import hashlib
import io

from ..yandex_disk import (
    ROOT,
    YandexDisk,
    YandexDiskError,
    configured_from_env,
    safe_path,
)


class LegacyMediaUnavailable(YandexDiskError):
    """The archive is retained; this reference needs a verified Disk mapping."""
    def __init__(self):
        super().__init__(
            'Архивный оригинал пока не подключён к Диску. История сохранена; нужна проверка переноса.',
            code='legacy_media_unavailable')


def standalone_media_mode(explicit=None):
    from ..config import config
    if explicit is not None and type(explicit) is not bool:
        raise ValueError('Explicit media mode required')
    # A stale legacy caller must never override the process-wide standalone gate.
    return getattr(config, 'standalone', False) is True or explicit is True


def direct_disk_mode(explicit=None):
    if standalone_media_mode(explicit):
        return True
    from ..push_notifications import app_channel
    return app_channel()


def disk_reference(file_id):
    if isinstance(file_id, str) and file_id.startswith('app:'):
        return safe_path(file_id)
    return None


def configured_storage():
    token, client_id = configured_from_env()
    if not token:
        raise YandexDiskError('Private media storage unavailable')
    return YandexDisk(token, client_id)


async def save_private_media(actor_id, raw, *, receipt=False):
    from .photo_storage import image_format

    if type(actor_id) is not int or actor_id <= 0:
        raise ValueError('Invalid media owner')
    if receipt:
        from .receipt_ai import document_format
        from .receipt_documents import MAGIC, manifest
        manifest(raw)
        ext, mime = ('pbr', 'application/vnd.photoboss.receipt-bundle') if raw.startswith(MAGIC) else document_format(raw)
    else:
        ext, mime = image_format(raw)
    digest = hashlib.sha256(raw).hexdigest()
    disk = configured_storage()
    for folder in (ROOT, ROOT + '/web-media', ROOT + f'/web-media/{actor_id}'):
        await disk.ensure_dir(folder)
    path = ROOT + f'/web-media/{actor_id}/{digest}.{ext}'
    # upload_bytes confirms the stored size and checksum before returning.
    await disk.upload_bytes(path, raw, content_type=mime)
    return path, 'web-sha256:' + digest


async def load_private_media(bot, file_id, *, max_bytes=20 * 1024 * 1024, timeout=40, standalone=None):
    standalone = standalone_media_mode(standalone)
    from ..disk_sale_media import FILE_PREFIX, load_reference
    if isinstance(file_id, str) and file_id.startswith(FILE_PREFIX):
        from ..db import Session
        async with Session() as session:
            return await load_reference(session, file_id, max_bytes=max_bytes)
    path = disk_reference(file_id)
    if path:
        return await configured_storage().download_bytes(path, max_bytes=max_bytes)
    if standalone or bot is None:
        # No getFile/download fallback and no invented replacement identifier.
        # A later verified archive mapping can resolve the original without
        # rewriting the immutable historical reference or deleting its record.
        raise LegacyMediaUnavailable()

    class LimitedBuffer(io.BytesIO):
        def write(self, data):
            if self.tell() + len(data) > max_bytes:
                raise ValueError('Image exceeds download size limit')
            return super().write(data)

    buffer = LimitedBuffer()
    await bot.download(file_id, destination=buffer, timeout=timeout)
    return buffer.getvalue()


async def telegram_media_input(bot, file_id):
    """Resolve private web media only after a Telegram handler's role/scope check."""
    if standalone_media_mode():
        raise RuntimeError('Telegram media delivery is disabled in standalone mode')
    from ..disk_sale_media import FILE_PREFIX
    if not disk_reference(file_id) and not (isinstance(file_id, str) and file_id.startswith(FILE_PREFIX)):
        return file_id
    from aiogram.types import BufferedInputFile

    from .photo_storage import image_format
    raw = await load_private_media(bot, file_id)
    ext, _ = image_format(raw)
    return BufferedInputFile(raw, filename='photo-boss.' + ext)


async def send_private_image(message, file_id, *, caption):
    from aiogram.exceptions import TelegramBadRequest

    media = await telegram_media_input(message.bot, file_id)
    try:
        return await message.answer_photo(media, caption=caption)
    except TelegramBadRequest as exc:
        reason = str(exc).casefold()
        if not any(value in reason for value in (
            'photo_invalid_dimensions', 'image_process_failed', 'photo_ext_invalid',
            'wrong file identifier', 'wrong file type', 'file_id not found',
        )):
            raise
        # Some saved originals/Telegram document IDs cannot be sent as a photo.
        # A document preserves all original bytes and remains reviewable.
        return await message.answer_document(media, caption=caption)
