"""Yandex.Disk storage safety checks. No provider network is used."""
import asyncio
import hashlib
import io
from unittest.mock import AsyncMock

import pytest

from app.yandex_disk import ROOT, YandexDisk, YandexDiskError, safe_path


@pytest.mark.parametrize('lost_response', [False, True])
def test_streamed_original_is_verified_by_size_and_checksum(monkeypatch, lost_response):
    from app import yandex_disk as module

    raw = b'camera-original' * 700000
    digest = hashlib.sha256(raw).hexdigest()
    md5 = hashlib.md5(raw, usedforsecurity=False).hexdigest()
    captured = {}

    class Response:
        status = 201

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self, **kwargs):
            captured['timeout'] = kwargs['timeout'].total

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def request(self, method, href, **kwargs):
            assert method == 'PUT' and href.startswith('https://')
            assert hasattr(kwargs['data'], 'read'), 'Provider must receive a stream'
            assert kwargs['data'].read() == raw
            assert kwargs['headers']['Content-Length'] == str(len(raw))
            if lost_response:
                raise asyncio.TimeoutError('lost acknowledgement')
            return Response()

    disk = YandexDisk('private-token')
    disk._json = AsyncMock(return_value={'href': 'https://upload.example.invalid/original', 'method': 'PUT'})
    metadata = {'type': 'file', 'size': len(raw), 'sha256': digest}
    disk.metadata = AsyncMock(return_value=metadata)
    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    with io.BytesIO(raw) as source:
        result = asyncio.run(disk.upload_file(ROOT + '/academy/original.jpg', source,
                            size=len(raw), sha256=digest, md5=md5, content_type='image/jpeg'))
    assert result == metadata and captured['timeout'] == 300
    disk.metadata.assert_awaited_once()


def test_streamed_original_mismatched_checksum_cannot_commit(monkeypatch):
    from app import yandex_disk as module

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            raise asyncio.TimeoutError('lost acknowledgement')

    disk = YandexDisk('private-token')
    disk._json = AsyncMock(return_value={'href': 'https://upload.example.invalid/original'})
    disk.metadata = AsyncMock(return_value={'type': 'file', 'size': 3, 'sha256': 'wrong'})
    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    with io.BytesIO(b'abc') as source, pytest.raises(YandexDiskError) as failure:
        asyncio.run(disk.upload_file(ROOT + '/academy/original.jpg', source,
                    size=3, sha256=hashlib.sha256(b'abc').hexdigest(), md5='unused'))
    assert failure.value.code == 'upload_timeout'


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
    assert metadata.value.code == "api_timeout"
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
    assert result['errorStage'] == 'read' and result['errorCode'] == 'probe_mismatch'
    assert result['checkedAt']
    disk.delete.assert_awaited_once_with(disk.upload_bytes.await_args.args[0])


def test_download_link_api_temporary_failure_is_retried():
    from app.yandex_disk import YandexDiskError
    disk = YandexDisk('token')
    disk._json = AsyncMock(side_effect=[YandexDiskError('Yandex.Disk HTTP 503', code='api_http', http_status=503),
                                       YandexDiskError('Yandex.Disk HTTP 404', code='api_http', http_status=404)])
    with pytest.raises(YandexDiskError, match='HTTP 404'):
        asyncio.run(disk.download_bytes(ROOT+'/photo.jpg'))
    assert disk._json.await_count == 2


def full_probe_disk():
    disk = YandexDisk('test-token-never-public')
    disk.ensure_dir = AsyncMock()
    disk.upload_bytes = AsyncMock()
    async def download(path, **kwargs):
        return disk.upload_bytes.await_args.args[1]
    disk.download_bytes = AsyncMock(side_effect=download)
    disk.delete = AsyncMock()
    return disk


def test_directory_only_check_keeps_last_full_evidence_until_connectivity_fails():
    async def run():
        disk = full_probe_disk()
        full = await disk.verify(write_test=True)
        connected = await disk.verify(write_test=False)
        assert connected['connected'] and connected['writeVerified'] and connected['readVerified']
        assert connected['fullCheckedAt'] == full['fullCheckedAt']
        assert disk.upload_bytes.await_count == disk.download_bytes.await_count == 1
        disk.ensure_dir.side_effect = YandexDiskError('hidden provider message', code='api_http', http_status=403)
        unavailable = await disk.verify(write_test=False)
        assert not any(unavailable[k] for k in ('connected', 'writeVerified', 'readVerified'))
        assert unavailable['fullCheckedAt'] == full['fullCheckedAt']
        assert unavailable['errorStage'] == 'root_directory' and unavailable['providerStatus'] == 403
    asyncio.run(run())


def test_directory_check_cannot_clear_failed_file_read_diagnostics():
    async def run():
        disk = full_probe_disk()
        disk.download_bytes = AsyncMock(side_effect=YandexDiskError('hidden network detail', code='download_timeout'))
        failed = await disk.verify()
        connected = await disk.verify(write_test=False)
        assert connected['connected'] and connected['writeVerified'] and not connected['readVerified']
        for key in ('error', 'errorStage', 'errorCode', 'providerStatus', 'fullCheckedAt'):
            assert connected[key] == failed[key]
        assert connected['errorStage'] == 'read' and connected['errorCode'] == 'download_timeout'
        disk.download_bytes.side_effect = lambda path, **kwargs: disk.upload_bytes.await_args.args[1]
        recovered = await disk.verify()
        assert recovered['connected'] and recovered['writeVerified'] and recovered['readVerified']
        assert all(recovered[key] is None for key in ('error', 'errorStage', 'errorCode', 'providerStatus'))
    asyncio.run(run())


@pytest.mark.parametrize('stage,expected_flags', [
    ('root_directory', (False, False, False)),
    ('system_directory', (False, False, False)),
    ('write', (True, False, False)),
    ('read', (True, True, False)),
])
def test_probe_diagnostics_identify_stage_without_exposing_exception(stage, expected_flags, caplog):
    disk = full_probe_disk()
    failure = YandexDiskError('https://private-provider.invalid/signed?token=TOP-SECRET',
                              code='api_http', http_status=503)
    if stage == 'root_directory':
        disk.ensure_dir.side_effect = failure
    elif stage == 'system_directory':
        disk.ensure_dir.side_effect = [{}, failure]
    elif stage == 'write':
        disk.upload_bytes.side_effect = failure
    else:
        disk.download_bytes.side_effect = failure
    result = asyncio.run(disk.verify())
    assert tuple(result[k] for k in ('connected', 'writeVerified', 'readVerified')) == expected_flags
    assert result['errorStage'] == stage and result['errorCode'] == 'api_http'
    assert result['providerStatus'] == 503 and result['fullCheckedAt']
    assert 'TOP-SECRET' not in str(result) + caplog.text
    assert 'private-provider' not in str(result) + caplog.text
    assert 'test-token-never-public' not in str(result) + caplog.text


def successful_download_session(monkeypatch):
    from app import yandex_disk as module
    calls = []
    class Body:
        async def iter_chunked(self, _size): yield b'original'
    class Response:
        status, headers, content = 200, {}, Body()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def get(self, href):
            calls.append(href)
            return Response()
    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    return calls


@pytest.mark.parametrize('code,status', [('api_timeout', None), ('api_network', None), ('api_http', 503)])
def test_download_link_transient_error_retries_once(monkeypatch, code, status):
    calls = successful_download_session(monkeypatch)
    disk = YandexDisk('token')
    disk._json = AsyncMock(side_effect=[
        YandexDiskError('hidden transport detail', code=code, http_status=status),
        {'href': 'https://download.example.invalid/file'},
    ])
    assert asyncio.run(disk.download_bytes(ROOT+'/photo.jpg')) == b'original'
    assert disk._json.await_count == 2 and len(calls) == 1


@pytest.mark.parametrize('status', [401, 403, 404])
def test_download_link_persistent_api_failure_is_not_retried(monkeypatch, status):
    calls = successful_download_session(monkeypatch)
    disk = YandexDisk('token')
    disk._json = AsyncMock(side_effect=YandexDiskError('hidden auth detail', code='api_http', http_status=status))
    with pytest.raises(YandexDiskError) as error:
        asyncio.run(disk.download_bytes(ROOT+'/photo.jpg'))
    assert error.value.http_status == status
    assert disk._json.await_count == 1 and not calls


def test_download_unsafe_target_is_not_requested_or_retried(monkeypatch):
    calls = successful_download_session(monkeypatch)
    disk = YandexDisk('token')
    disk._json = AsyncMock(return_value={'href': 'http://untrusted.invalid/file'})
    with pytest.raises(YandexDiskError) as error:
        asyncio.run(disk.download_bytes(ROOT+'/photo.jpg'))
    assert error.value.code == 'unsafe_target'
    assert disk._json.await_count == 1 and not calls


@pytest.mark.parametrize('configured,healthy,expected', [(True, False, 1), (True, True, 0), (False, False, 0)])
def test_recovery_loop_only_rechecks_configured_degraded_state(monkeypatch, configured, healthy, expected):
    from app import yandex_disk as module
    async def run():
        disk = YandexDisk('token' if configured else '')
        disk.state.update(connected=healthy, writeVerified=healthy, readVerified=healthy)
        async def recover(**kwargs):
            disk.state.update(connected=True, writeVerified=True, readVerified=True)
        disk.verify = AsyncMock(side_effect=recover)
        sleeps = []
        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == 2:
                raise asyncio.CancelledError
        monkeypatch.setattr(module.asyncio, 'sleep', sleep)
        with pytest.raises(asyncio.CancelledError):
            await module.storage_recovery_loop(disk)
        assert disk.verify.await_count == expected
        if expected:
            disk.verify.assert_awaited_once_with(write_test=True)
        assert sleeps == [300, 300]
    asyncio.run(run())


def test_recovery_loop_contains_provider_errors_and_preserves_cancellation(monkeypatch, caplog):
    from app import yandex_disk as module
    async def run():
        disk = YandexDisk('token')
        disk.verify = AsyncMock(side_effect=[OSError('TOP-SECRET provider URL'), asyncio.CancelledError()])
        sleeps = []
        async def sleep(delay): sleeps.append(delay)
        monkeypatch.setattr(module.asyncio, 'sleep', sleep)
        with pytest.raises(asyncio.CancelledError):
            await module.storage_recovery_loop(disk)
        assert disk.verify.await_count == 2 and sleeps == [300]
        assert 'TOP-SECRET' not in caplog.text
    asyncio.run(run())


@pytest.mark.parametrize('interval', [0, -1, False, float('nan')])
def test_recovery_loop_rejects_busy_loop_intervals(interval):
    from app.yandex_disk import storage_recovery_loop
    with pytest.raises(ValueError):
        asyncio.run(storage_recovery_loop(YandexDisk('token'), interval=interval))


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
