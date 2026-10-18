"""Yandex.Disk storage safety checks. No provider network is used."""
import asyncio
import hashlib
from unittest.mock import AsyncMock

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

    async def download(path, **_):
        calls.append(("download", path))
        return calls[2][2]

    monkeypatch.setattr(storage, "ensure_dir", ensure)
    monkeypatch.setattr(storage, "upload_bytes", upload)
    monkeypatch.setattr(storage, "delete", delete)
    monkeypatch.setattr(storage, "download_bytes", download)
    result = asyncio.run(storage.verify(write_test=True))
    assert result["connected"] is True
    assert result["writeVerified"] is True
    assert result["readVerified"] is True
    assert calls[0] == ("dir", ROOT)
    assert calls[1][0] == "dir"
    assert calls[2][0] == "upload"
    assert calls[3][0] == "download"
    assert calls[4][0] == "delete"

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

@pytest.mark.parametrize('first_status', [403, 429, 500])
def test_transient_download_retries_with_new_link(monkeypatch, first_status):
    """A temporary CDN error should not permanently break Academy photo previews."""
    from app import yandex_disk as module
    calls = {"link": 0, "download": 0}

    class Body:
        async def iter_chunked(self, _size):
            yield b"original"

    class Response:
        def __init__(self, status):
            self.status = status
            self.headers = {}
            self.content = Body()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, _href):
            calls["download"] += 1
            return Response(first_status if calls["download"] == 1 else 200)

    async def link(*args, **kwargs):
        calls["link"] += 1
        return {"href": "https://download.example.invalid/image"}

    disk = YandexDisk("token")
    monkeypatch.setattr(disk, "_json", link)
    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    assert asyncio.run(disk.download_bytes(ROOT + "/shootings/test.jpg")) == b"original"
    assert calls == {"link": 2, "download": 2}


@pytest.mark.parametrize('kind', ['dir', 'file'])
def test_existing_file_is_not_accepted_as_a_directory(kind):
    from app.yandex_disk import YandexDiskError
    disk = YandexDisk('token')
    disk._json = AsyncMock(side_effect=YandexDiskError('Yandex.Disk HTTP 409'))
    disk.metadata = AsyncMock(return_value={'type': kind})
    if kind == 'dir':
        assert asyncio.run(disk.ensure_dir(ROOT)) == {'type': 'dir'}
    else:
        with pytest.raises(YandexDiskError, match='Вместо папки'):
            asyncio.run(disk.ensure_dir(ROOT))
    disk.metadata.assert_awaited_once_with(ROOT)


@pytest.mark.parametrize('saved', [True, False])
def test_lost_upload_acknowledgement_requires_matching_remote_checksum(monkeypatch, saved):
    from app import yandex_disk as module
    calls = []

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def request(self, method, href, **kwargs):
            calls.append((method, kwargs['data']))
            raise asyncio.TimeoutError('private provider URL')

    disk = YandexDisk('secret-token-value')
    disk._json = AsyncMock(return_value={'href': 'https://upload.example.invalid/file'})
    disk.metadata = AsyncMock(return_value={'type': 'file', 'size': 3,
        'sha256': hashlib.sha256(b'abc' if saved else b'bad').hexdigest()})
    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    if saved:
        assert asyncio.run(disk.upload_bytes(ROOT+'/photo.jpg', b'abc'))['size'] == 3
    else:
        with pytest.raises(module.YandexDiskError, match='подтвердить загрузку') as error:
            asyncio.run(disk.upload_bytes(ROOT+'/photo.jpg', b'abc'))
        assert 'private provider URL' not in str(error.value)
    assert calls == [('PUT', b'abc')]
    disk.metadata.assert_awaited_once_with(ROOT+'/photo.jpg')


def test_simultaneous_probes_use_unique_files_and_leave_existing_marker_intact():
    async def run():
        disk = YandexDisk('token')
        old = ROOT + '/system/connection-check.txt'
        files, writes = {old: b'existing file'}, []
        disk.ensure_dir = AsyncMock()
        async def upload(path, payload, **kwargs):
            files[path] = payload
            writes.append(path)
            await asyncio.sleep(0)
        async def download(path, **kwargs): return files[path]
        async def delete(path): files.pop(path)
        disk.upload_bytes, disk.download_bytes, disk.delete = upload, download, delete
        results = await asyncio.gather(disk.verify(), disk.verify())
        assert all(r['connected'] and r['writeVerified'] and r['readVerified'] and not r['error'] for r in results)
        assert len(set(writes)) == 2
        assert files == {old: b'existing file'}
    asyncio.run(run())


def test_corrupt_probe_read_is_reported_and_its_file_is_cleaned_up():
    disk = YandexDisk('token')
    disk.ensure_dir = AsyncMock()
    disk.upload_bytes = AsyncMock()
    disk.download_bytes = AsyncMock(return_value=b'wrong')
    disk.delete = AsyncMock()
    result = asyncio.run(disk.verify())
    assert result['writeVerified'] and not result['readVerified'] and result['error']
    assert result['checkedAt']
    disk.delete.assert_awaited_once_with(disk.upload_bytes.await_args.args[0])


def test_download_link_api_temporary_failure_is_retried():
    from app.yandex_disk import YandexDiskError
    disk = YandexDisk('token')
    disk._json = AsyncMock(side_effect=[YandexDiskError('Yandex.Disk HTTP 503'), YandexDiskError('Yandex.Disk HTTP 404')])
    with pytest.raises(YandexDiskError, match='HTTP 404'):
        asyncio.run(disk.download_bytes(ROOT+'/photo.jpg'))
    assert disk._json.await_count == 2


@pytest.mark.parametrize('invalid_json', [False, True])
def test_malformed_success_response_is_recoverable_and_does_not_expose_contents(monkeypatch, invalid_json):
    from app import yandex_disk as module
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self, **kwargs):
            if invalid_json:
                raise module.json.JSONDecodeError('private provider details', '<html>', 0)
            return ['private provider details']
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def request(self, *args, **kwargs): return Response()
    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    with pytest.raises(module.YandexDiskError, match='некорректный ответ') as error:
        asyncio.run(YandexDisk('secret-token-value').metadata(ROOT))
    assert 'private provider details' not in str(error.value)
    assert 'secret-token-value' not in str(error.value)


@pytest.mark.parametrize('status', [201, 202, 204, 404])
def test_bodyless_accepted_response_does_not_report_false_cleanup_failure(monkeypatch, status):
    from app import yandex_disk as module
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self, **kwargs): return None
    response = Response()
    response.status = status
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def request(self, *args, **kwargs): return response
    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    disk = YandexDisk('token')
    if status == 201:
        assert asyncio.run(disk.ensure_dir(ROOT+'/system')) == {}
    else:
        assert asyncio.run(disk.delete(ROOT+'/system/test.txt')) is None
