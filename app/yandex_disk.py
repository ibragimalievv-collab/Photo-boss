"""Yandex.Disk app-folder storage for Photo Boss.

The OAuth token stays in server environment variables. Paths are restricted to
the app-folder namespace and provider upload URLs are never logged or returned.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from .miniapp_security import AccessError

logger = logging.getLogger(__name__)
API = "https://cloud-api.yandex.net/v1/disk"
ROOT = "app:/PhotoBoss"
SYSTEM = ROOT + "/system"
_PATH_RE = re.compile(r"^app:/PhotoBoss(?:/[A-Za-z0-9._-]+)*$")


class YandexDiskError(RuntimeError):
    pass


def safe_path(path: str) -> str:
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path) or "/../" in path or path.endswith("/.."):
        raise ValueError("Unsafe Yandex.Disk app-folder path")
    return path


def configured_from_env() -> tuple[str, str]:
    return (
        os.getenv("YANDEX_DISK_TOKEN", "").strip(),
        os.getenv("YANDEX_DISK_CLIENT_ID", "").strip(),
    )


class YandexDisk:
    def __init__(self, token: str, client_id: str = ""):
        self.token = token
        self.client_id = client_id
        self.state = {
            "provider": "yandex_disk",
            "configured": bool(token),
            "connected": False,
            "writeVerified": False,
            "root": ROOT,
            "checkedAt": None,
            "error": None,
        }

    @property
    def headers(self):
        return {"Authorization": f"OAuth {self.token}", "Accept": "application/json"}

    async def _json(self, method: str, endpoint: str, *, params=None, ok=(200,)):
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, API + endpoint, headers=self.headers, params=params) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {}
                if response.status not in ok:
                    code = data.get("error") if isinstance(data, dict) else None
                    message = data.get("message") if isinstance(data, dict) else None
                    detail = ": ".join(x for x in (code, message) if isinstance(x, str) and x)[:300]
                    raise YandexDiskError(f"Yandex.Disk HTTP {response.status}" + (f": {detail}" if detail else ""))
                return data

    async def metadata(self, path: str):
        return await self._json("GET", "/resources", params={"path": safe_path(path)})

    async def ensure_dir(self, path: str):
        path = safe_path(path)
        try:
            return await self._json("PUT", "/resources", params={"path": path}, ok=(201,))
        except YandexDiskError as exc:
            if "HTTP 409" in str(exc):
                return {"exists": True}
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
            raise YandexDiskError("Yandex.Disk did not return a valid upload target")
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, href, data=payload, headers={"Content-Type": content_type}) as response:
                if response.status not in (200, 201, 202):
                    raise YandexDiskError(f"Yandex.Disk upload HTTP {response.status}")
        return await self.metadata(path)

    async def delete(self, path: str):
        path = safe_path(path)
        await self._json(
            "DELETE", "/resources",
            params={"path": path, "permanently": "true", "force_async": "false"},
            ok=(202, 204),
        )

    def public_status(self):
        return dict(self.state)

    async def verify(self, *, write_test=True):
        now = datetime.now(timezone.utc).isoformat()
        self.state.update(checkedAt=now, connected=False, writeVerified=False, error=None)
        if not self.token:
            self.state["error"] = "YANDEX_DISK_TOKEN is not configured"
            logger.warning("Yandex.Disk is not configured")
            return self.public_status()
        marker = SYSTEM + "/connection-check.txt"
        try:
            await self.metadata(ROOT.rsplit("/", 1)[0] + "/") if False else None
            # app:/ itself is outside safe_path(ROOT); metadata ROOT will be available after creation.
            try:
                await self.ensure_dir(ROOT)
            except YandexDiskError as exc:
                # If the app folder root itself is exposed but nested creation is forbidden,
                # surface the provider error without leaking OAuth material.
                raise exc
            await self.ensure_dir(SYSTEM)
            self.state["connected"] = True
            if write_test:
                await self.upload_bytes(
                    marker,
                    b"Photo Boss Yandex.Disk connectivity check\n",
                    content_type="text/plain; charset=utf-8",
                )
                self.state["writeVerified"] = True
                try:
                    await self.delete(marker)
                except YandexDiskError:
                    logger.warning("Yandex.Disk write probe succeeded but cleanup failed")
            logger.info(
                "Yandex.Disk app-folder verified: connected=%s write=%s root=%s",
                self.state["connected"], self.state["writeVerified"], ROOT,
            )
        except Exception as exc:
            self.state["error"] = str(exc)[:300]
            logger.warning("Yandex.Disk verification failed: %s", self.state["error"])
        return self.public_status()


async def storage_status(request):
    actor = request["miniapp_actor"]
    if "OWNER" not in actor["roles"]:
        raise AccessError("Состояние хранилища доступно только владельцу.")
    storage: YandexDisk = request.app["yandex_disk"]
    return web.json_response(storage.public_status())


async def storage_probe(request):
    actor = request["miniapp_actor"]
    if "OWNER" not in actor["roles"]:
        raise AccessError("Проверять хранилище может только владелец.")
    storage: YandexDisk = request.app["yandex_disk"]
    return web.json_response(await storage.verify(write_test=True))


def install_yandex_disk(app):
    token, client_id = configured_from_env()
    storage = YandexDisk(token, client_id)
    app["yandex_disk"] = storage
    app.router.add_get("/api/miniapp/storage/status", storage_status)
    app.router.add_post("/api/miniapp/storage/probe", storage_probe)
    return storage
