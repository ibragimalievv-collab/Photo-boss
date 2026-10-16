"""Yandex.Disk storage safety checks. No provider network is used."""
import asyncio

import pytest

from app.yandex_disk import ROOT, YandexDisk, safe_path


@pytest.mark.parametrize("path", [
    ROOT,
    ROOT + "/system",
    ROOT + "/shootings/123/abc_1.jpg",
])
def test_safe_app_folder_paths(path):
    assert safe_path(path) == path


@pytest.mark.parametrize("path", [
    "", "disk:/PhotoBoss", "app:/", "app:/Other", ROOT + "/../secret",
    ROOT + "/x y", ROOT + "/x?token=1", ROOT + "//x",
])
def test_unsafe_paths_rejected(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_public_status_never_contains_credentials():
    storage = YandexDisk("secret-token-value", "client-id-value")
    payload = storage.public_status()
    assert "secret-token-value" not in str(payload)
    assert "client-id-value" not in str(payload)
    assert payload["root"] == ROOT


def test_verify_write_sequence_without_network(monkeypatch):
    storage = YandexDisk("token")
    calls = []

    async def ensure(path):
        calls.append(("dir", path))
        return {}

    async def upload(path, payload, **_):
        calls.append(("upload", path, payload))
        return {"type": "file"}

    async def delete(path):
        calls.append(("delete", path))

    monkeypatch.setattr(storage, "ensure_dir", ensure)
    monkeypatch.setattr(storage, "upload_bytes", upload)
    monkeypatch.setattr(storage, "delete", delete)
    result = asyncio.run(storage.verify(write_test=True))
    assert result["connected"] is True
    assert result["writeVerified"] is True
    assert calls[0] == ("dir", ROOT)
    assert calls[1][0] == "dir"
    assert calls[2][0] == "upload"
    assert calls[3][0] == "delete"

def test_provider_network_timeouts_have_safe_recoverable_errors(monkeypatch):
    """Real provider timeouts must not become unhandled application crashes."""
    from app import yandex_disk as module

    class TimeoutSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            raise asyncio.TimeoutError("private network detail")

        def get(self, *args, **kwargs):
            raise asyncio.TimeoutError("private network detail")

    monkeypatch.setattr(module.aiohttp, "ClientSession", TimeoutSession)
    disk = YandexDisk("secret-token-value")

    with pytest.raises(module.YandexDiskError, match="не ответил") as metadata:
        asyncio.run(disk._json("GET", "/resources"))
    assert "private network detail" not in str(metadata.value)
    assert "secret-token-value" not in str(metadata.value)

    async def upload_url(*args, **kwargs):
        return {"href": "https://upload.example.invalid/test", "method": "PUT"}

    monkeypatch.setattr(disk, "_json", upload_url)
    with pytest.raises(module.YandexDiskError, match="подтвердить загрузку"):
        asyncio.run(disk.upload_bytes(ROOT + "/shootings/test.jpg", b"original JPEG"))

    async def download_url(*args, **kwargs):
        return {"href": "https://download.example.invalid/test"}

    monkeypatch.setattr(disk, "_json", download_url)
    with pytest.raises(module.YandexDiskError, match="прервал скачивание"):
        asyncio.run(disk.download_bytes(ROOT + "/shootings/test.jpg"))
