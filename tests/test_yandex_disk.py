"""Yandex.Disk storage safety checks. No provider network is used."""
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


@pytest.mark.asyncio
async def test_verify_write_sequence_without_network(monkeypatch):
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
    result = await storage.verify(write_test=True)
    assert result["connected"] is True
    assert result["writeVerified"] is True
    assert calls[0] == ("dir", ROOT)
    assert calls[1][0] == "dir"
    assert calls[2][0] == "upload"
    assert calls[3][0] == "delete"
