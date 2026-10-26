"""Yandex.Disk app-folder storage for Photo Boss.

The OAuth token stays in server environment variables. Paths are restricted to
the app-folder namespace and provider upload URLs are never logged or returned.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import uuid
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from .miniapp_security import AccessError

logger = logging.getLogger(__name__)
API = "https://cloud-api.yandex.net/v1/disk"
ROOT = "app:/PhotoBoss"
SYSTEM = ROOT + "/system"
_PATH_RE = re.compile(r"^app:/PhotoBoss(?:/[A-Za-zА-Яа-яЁё0-9._()#-]+|/Все фото)*$")


class YandexDiskError(RuntimeError):
    CODES = frozenset({
        "provider_error", "api_http", "api_timeout", "api_network", "invalid_response",
        "unsafe_target", "integrity", "upload_http", "upload_timeout", "upload_network",
        "download_http", "download_timeout", "download_network", "download_limit",
        "probe_mismatch", "not_configured", "validation",
    })

    def __init__(self, message, *, code="provider_error", http_status=None):
        super().__init__(message)
        self.code = code if code in self.CODES else "provider_error"
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None


def safe_path(path: str) -> str:
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
        raise ValueError("Unsafe Yandex.Disk app-folder path")
    parts = path[len("app:/PhotoBoss"):].split("/")
    if any(part in {".", ".."} for part in parts if part):
        raise ValueError("Unsafe Yandex.Disk app-folder path")
    return path


def safe_disk_read_path(path: str) -> str:
    """Permit private Disk reads without granting any external-folder writes."""
    if (not isinstance(path, str) or not path.startswith('disk:/') or len(path) > 1000
            or '\\' in path or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise ValueError('Укажите папку подключённого Яндекс.Диска.')
    parts = path[len('disk:/'):].split('/')
    if path != 'disk:/' and any(part in {'', '.', '..'} for part in parts):
        raise ValueError('Некорректный путь папки Яндекс.Диска.')
    return path


def configured_from_env() -> tuple[str, str]:
    return (
        os.getenv("YANDEX_DISK_TOKEN", "").strip(),
        os.getenv("YANDEX_DISK_CLIENT_ID", "").strip(),
    )


def validate_saved(metadata, payload):
    """A successful PUT alone is not proof that the expected original was saved."""
    validate_saved_digest(metadata, len(payload), hashlib.sha256(payload).hexdigest(),
                          hashlib.md5(payload, usedforsecurity=False).hexdigest())


def validate_saved_digest(metadata, size, sha256, md5):
    """Validate a streamed original without making an in-memory byte copy."""
    if not isinstance(metadata, dict) or metadata.get('type') != 'file' or metadata.get('size') != size:
        raise YandexDiskError('Сохранение файла ещё не подтверждено: размер не совпадает.', code="integrity")
    digest = metadata.get('sha256')
    valid = digest == sha256 if digest else metadata.get('md5') == md5
    if not valid:
        raise YandexDiskError('Сохранение файла ещё не подтверждено: контрольная сумма не совпадает.', code="integrity")


class YandexDisk:
    def __init__(self, token: str, client_id: str = ""):
        self.token = token
        self.client_id = client_id
        self._verify_lock = asyncio.Lock()
        self.state = {
            "provider": "yandex_disk",
            "configured": bool(token),
            "connected": False,
            "writeVerified": False,
            "readVerified": False,
            "root": ROOT,
            "checkedAt": None,
            "fullCheckedAt": None,
            "error": None,
            "errorStage": None,
            "errorCode": None,
            "providerStatus": None,
        }

    @property
    def headers(self):
        return {"Accept": "application/json", **({"Authorization": f"OAuth {self.token}"} if self.token else {})}

    async def _json(self, method: str, endpoint: str, *, params=None, ok=(200,)):
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.request(method, API + endpoint, headers=self.headers, params=params) as response,
            ):
                malformed = False
                try:
                    data = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError):
                    data = {}
                    malformed = True
                if response.status not in ok:
                    code = data.get("error") if isinstance(data, dict) else None
                    detail = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", code) else "provider_error"
                    raise YandexDiskError(f"Yandex.Disk HTTP {response.status}" + (f": {detail}" if detail else ""),
                                          code="api_http", http_status=response.status)
                # aiohttp returns None (rather than raising JSONDecodeError)
                # for an empty 204 body. DELETE's bodyless acknowledgements,
                # including an already absent file, are valid results.
                if data is None and response.status != 200:
                    return {}
                if not isinstance(data, dict) or (malformed and response.status == 200):
                    raise YandexDiskError("Яндекс.Диск вернул некорректный ответ. Повторите попытку.", code="invalid_response")
                return data
        except asyncio.TimeoutError as exc:
            raise YandexDiskError("Яндекс.Диск не ответил на запрос. Повторите попытку.", code="api_timeout") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise YandexDiskError("Яндекс.Диск не ответил на запрос. Повторите попытку.", code="api_network") from exc
    async def metadata(self, path: str):
        return await self._json("GET", "/resources", params={"path": safe_path(path)})

    async def list_directory(self, path: str, *, page_size=200, max_items=10000):
        """Read all pages; unavailable/incomplete is never reported as empty."""
        return await self._read_directory(safe_path(path), page_size=page_size, max_items=max_items)

    async def disk_metadata(self, path: str):
        return await self._json('GET', '/resources', params={'path': safe_disk_read_path(path), 'limit': 0})

    async def disk_list_directory(self, path: str, *, page_size=200, max_items=10000):
        return await self._read_directory(safe_disk_read_path(path), page_size=page_size, max_items=max_items)

    async def public_metadata(self, key, path='/'):
        return await self._json('GET', '/public/resources', params={'public_key': key, 'path': path, 'limit': 0})

    async def public_list_directory(self, key, path='/', *, page_size=200, max_items=10000):
        return await self._read_directory(path, page_size=page_size, max_items=max_items,
                                          endpoint='/public/resources', extra={'public_key': key})

    async def public_download_bytes(self, key, path='/', *, max_bytes=20 * 1024 * 1024):
        return await self._download_bytes(path, max_bytes=max_bytes, endpoint='/public/resources/download', extra={'public_key': key})

    async def _read_directory(self, path, *, page_size, max_items, endpoint='/resources', extra=None):
        items, names, expected_total = [], set(), None
        while True:
            data = await self._json('GET', endpoint, params={
                **(extra or {}), 'path': path, 'limit': page_size, 'offset': len(items), 'sort': 'name'})
            embedded = data.get('_embedded')
            if data.get('type') != 'dir' or not isinstance(embedded, dict):
                raise YandexDiskError('Не удалось прочитать список файлов папки.')
            page, total = embedded.get('items'), embedded.get('total')
            if not isinstance(page, list) or type(total) is not int or total < 0:
                raise YandexDiskError('Неполный ответ списка файлов Яндекс.Диска.')
            if expected_total is not None and total != expected_total:
                raise YandexDiskError('Список файлов изменился во время проверки. Повторите сверку.')
            expected_total = total
            for item in page:
                name = item.get('name') if isinstance(item, dict) else None
                if not isinstance(name, str) or name in names or '/' in name or name in {'.', '..'}:
                    raise YandexDiskError('Список файлов изменился во время проверки. Повторите сверку.')
                names.add(name)
                items.append(item)
            if len(items) > max_items:
                raise YandexDiskError('Папка превышает лимит сверки. Нужна проверка по частям.')
            if len(items) == total:
                return items
            if not page or len(items) > total:
                raise YandexDiskError('Список файлов изменился во время проверки. Повторите сверку.')

    async def ensure_dir(self, path: str):
        path = safe_path(path)
        try:
            return await self._json("PUT", "/resources", params={"path": path}, ok=(201,))
        except YandexDiskError as exc:
            if "HTTP 409" in str(exc):
                # A conflict may also mean a regular file occupies this path.
                # Do not claim connectivity or proceed to uploads in that case.
                existing = await self.metadata(path)
                if existing.get("type") == "dir":
                    return existing
                raise YandexDiskError("Вместо папки Photo Boss на Диске находится файл. Проверьте путь к хранилищу.") from exc
            raise

    async def upload_bytes(self, path: str, payload: bytes, *, content_type="application/octet-stream"):
        path = safe_path(path)
        data = await self._json(
            "GET", "/resources/upload",
            params={"path": path, "overwrite": "true"},
        )
        href = data.get("href") if isinstance(data, dict) else None
        method = data.get("method", "PUT") if isinstance(data, dict) else "PUT"
        if not isinstance(href, str) or not href.startswith("https://") or method not in {"PUT", "POST"}:
            raise YandexDiskError("Yandex.Disk did not return a valid upload target", code="unsafe_target")
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.request(method, href, data=payload, headers={"Content-Type": content_type}) as response,
            ):
                if response.status not in (200, 201, 202):
                    raise YandexDiskError(f"Yandex.Disk upload HTTP {response.status}", code="upload_http", http_status=response.status)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            # PUT may have reached the provider before the response was lost.
            # Recover a lost acknowledgement only when size AND checksum match.
            try:
                metadata = await self.metadata(path)
                validate_saved(metadata, payload)
                return metadata
            except YandexDiskError:
                pass
            code = "upload_timeout" if isinstance(exc, asyncio.TimeoutError) else "upload_network"
            raise YandexDiskError("Не удалось подтвердить загрузку на Яндекс.Диск. Повторите попытку.", code=code) from exc
        metadata = await self.metadata(path)
        validate_saved(metadata, payload)
        return metadata
    async def download_bytes(self, path: str, *, max_bytes=20 * 1024 * 1024):
        return await self._download_bytes(safe_path(path), max_bytes=max_bytes)

    async def upload_file(self, path, source, *, size, sha256, md5, content_type="application/octet-stream"):
        """Stream a seekable private original and confirm provider size/checksum."""
        path = safe_path(path)
        data = await self._json("GET", "/resources/upload", params={"path": path, "overwrite": "true"})
        href = data.get("href") if isinstance(data, dict) else None
        method = data.get("method", "PUT") if isinstance(data, dict) else "PUT"
        if not isinstance(href, str) or not href.startswith("https://") or method not in {"PUT", "POST"}:
            raise YandexDiskError("Yandex.Disk did not return a valid upload target", code="unsafe_target")
        source.seek(0)
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session,
                session.request(method, href, data=source,
                                headers={"Content-Type": content_type, "Content-Length": str(size)}) as response,
            ):
                if response.status not in (200, 201, 202):
                    raise YandexDiskError(f"Yandex.Disk upload HTTP {response.status}",
                                          code="upload_http", http_status=response.status)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            # A lost acknowledgement must not turn into a duplicate upload.
            try:
                metadata = await self.metadata(path)
                validate_saved_digest(metadata, size, sha256, md5)
                return metadata
            except YandexDiskError:
                pass
            code = "upload_timeout" if isinstance(exc, asyncio.TimeoutError) else "upload_network"
            raise YandexDiskError("Не удалось подтвердить загрузку на Яндекс.Диск. Повторите попытку.", code=code) from exc
        metadata = await self.metadata(path)
        validate_saved_digest(metadata, size, sha256, md5)
        return metadata

    async def disk_download_bytes(self, path: str, *, max_bytes=20 * 1024 * 1024):
        return await self._download_bytes(safe_disk_read_path(path), max_bytes=max_bytes)

    async def _download_bytes(self, path, *, max_bytes, endpoint='/resources/download', extra=None):
        for attempt in range(2):
            # Fetch a fresh temporary link when Yandex returns a transient 5xx/429.
            # Retrying a read cannot duplicate files or financial operations.
            try:
                data = await self._json("GET", endpoint, params={**(extra or {}), "path": path})
            except YandexDiskError as exc:
                if attempt == 0 and (exc.code in {"api_timeout", "api_network"}
                                     or exc.http_status in {429, 500, 502, 503, 504}):
                    await asyncio.sleep(0.2)
                    continue
                raise
            href = data.get("href") if isinstance(data, dict) else None
            if not isinstance(href, str) or not href.startswith("https://"):
                raise YandexDiskError("Yandex.Disk did not return a valid download target", code="unsafe_target")
            timeout = aiohttp.ClientTimeout(total=45)
            payload = bytearray()
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.get(href) as response,
                ):
                    if response.status in (401, 403, 429, 500, 502, 503, 504) and attempt == 0:
                        await asyncio.sleep(0.2)
                        continue
                    if response.status != 200:
                        raise YandexDiskError(f"Yandex.Disk download HTTP {response.status}", code="download_http", http_status=response.status)
                    length = response.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        raise YandexDiskError("Yandex.Disk file exceeds download limit", code="download_limit")
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        payload.extend(chunk)
                        if len(payload) > max_bytes:
                            raise YandexDiskError("Yandex.Disk file exceeds download limit", code="download_limit")
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
                code = "download_timeout" if isinstance(exc, asyncio.TimeoutError) else "download_network"
                raise YandexDiskError("Яндекс.Диск прервал скачивание файла. Повторите запрос.", code=code) from exc
            return bytes(payload)
        raise YandexDiskError("Yandex.Disk download temporarily unavailable")

    async def delete(self, path: str):
        path = safe_path(path)
        await self._json(
            "DELETE", "/resources",
            params={"path": path, "permanently": "true", "force_async": "false"},
            ok=(202, 204, 404),
        )

    def public_status(self):
        return dict(self.state)

    async def delete_confirmed(self, path: str):
        await self.delete(path)
        # DELETE may return 202; never remove local references until storage confirms absence.
        try:
            await self.metadata(path)
        except YandexDiskError as exc:
            if 'HTTP 404' in str(exc):
                return
            raise
        raise YandexDiskError('Deletion is still pending')

    async def verify(self, *, write_test=True):
        # Owner probes and background recovery may run at the same time.
        async with self._verify_lock:
            return await self._verify(write_test=write_test)

    async def _verify(self, *, write_test):
        # Directory-only checks must not erase evidence from the last full
        # probe. A new full probe clears and recomputes all file evidence.
        file_error = {key: self.state.get(key) for key in ("error", "errorStage", "errorCode", "providerStatus")}
        retain_file_error = (not write_test and file_error["errorStage"] in {"write", "read"}
                             and not (self.state.get("writeVerified") and self.state.get("readVerified")))
        self.state.update(connected=False, error=None, errorStage=None, errorCode=None, providerStatus=None)
        if write_test:
            self.state.update(writeVerified=False, readVerified=False)
        if not self.token:
            self.state.update(writeVerified=False, readVerified=False,
                              errorStage="configuration", errorCode="not_configured")
            self.state["error"] = "YANDEX_DISK_TOKEN is not configured"
            self.state["checkedAt"] = datetime.now(timezone.utc).isoformat()
            if write_test:
                self.state["fullCheckedAt"] = self.state["checkedAt"]
            logger.warning("Yandex.Disk is not configured")
            return self.public_status()
        marker = SYSTEM + f"/connection-check-{uuid.uuid4().hex}.txt"
        payload = b"Photo Boss Yandex.Disk connectivity check\n" + uuid.uuid4().hex.encode()
        attempted_write = False
        stage = "root_directory"
        try:
            await self.ensure_dir(ROOT)
            stage = "system_directory"
            await self.ensure_dir(SYSTEM)
            self.state["connected"] = True
            if write_test:
                stage = "write"
                attempted_write = True
                await self.upload_bytes(
                    marker, payload,
                    content_type="text/plain; charset=utf-8",
                )
                self.state["writeVerified"] = True
                stage = "read"
                if await self.download_bytes(marker, max_bytes=1024) != payload:
                    raise YandexDiskError("Проверочный файл прочитан с ошибкой. Повторите проверку Диска.", code="probe_mismatch")
                self.state["readVerified"] = True
            elif retain_file_error:
                self.state.update(file_error)
            logger.info(
                "Yandex.Disk app-folder check: full=%s connected=%s write=%s read=%s root=%s",
                write_test, self.state["connected"], self.state["writeVerified"], self.state["readVerified"], ROOT,
            )
        except (YandexDiskError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            if not self.state["connected"]:
                self.state.update(writeVerified=False, readVerified=False)
            self.state["error"] = "Хранилище недоступно или сохранение не подтверждено."
            code = (exc.code if isinstance(exc, YandexDiskError)
                    else "api_timeout" if isinstance(exc, asyncio.TimeoutError)
                    else "validation" if isinstance(exc, ValueError) else "api_network")
            status = exc.http_status if isinstance(exc, YandexDiskError) else None
            self.state.update(errorStage=stage, errorCode=code, providerStatus=status)
            logger.warning("Yandex.Disk verification failed: stage=%s code=%s http=%s", stage, code, status)
        finally:
            if attempted_write:
                try:
                    await self.delete(marker)
                except YandexDiskError:
                    logger.warning("Yandex.Disk probe cleanup failed; only its temporary marker may remain")
            self.state["checkedAt"] = datetime.now(timezone.utc).isoformat()
            if write_test:
                self.state["fullCheckedAt"] = self.state["checkedAt"]
        return self.public_status()


async def storage_recovery_loop(storage, *, interval=300):
    """Recheck degraded storage independently of photo jobs and database access."""
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 1:
        raise ValueError("Storage recovery interval must be at least one second")
    while True:
        try:
            state = storage.public_status()
            if state.get("configured") and not all(state.get(key) for key in ("connected", "writeVerified", "readVerified")):
                await storage.verify(write_test=True)
        except asyncio.CancelledError:
            raise
        except (YandexDiskError, aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            # This loop must never log raw provider exceptions or signed links.
            logger.warning("Yandex.Disk recovery probe failed: code=probe_unavailable")
        await asyncio.sleep(interval)


async def storage_status(request):
    actor = request["miniapp_actor"]
    if "OWNER" not in actor["roles"]:
        raise AccessError("Состояние хранилища доступно только владельцу.")
    storage: YandexDisk = request.app["yandex_disk"]
    return web.json_response(storage.public_status(), headers={"Cache-Control": "no-store"})


async def storage_probe(request):
    actor = request["miniapp_actor"]
    if "OWNER" not in actor["roles"]:
        raise AccessError("Проверять хранилище может только владелец.")
    storage: YandexDisk = request.app["yandex_disk"]
    return web.json_response(await storage.verify(write_test=True), headers={"Cache-Control": "no-store"})


def install_yandex_disk(app):
    token, client_id = configured_from_env()
    storage = YandexDisk(token, client_id)
    app["yandex_disk"] = storage
    app.router.add_get("/api/miniapp/storage/status", storage_status)
    app.router.add_post("/api/miniapp/storage/probe", storage_probe)
    return storage
