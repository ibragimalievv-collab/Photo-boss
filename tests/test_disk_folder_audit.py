import asyncio
import hashlib
import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select
from test_team import Request
from test_workflow_services import fixture

from app.disk_folder_audit import (
    PREFIX,
    DiskFolderAudits,
    process_disk_audit,
    public_job,
)
from app.miniapp_security import AccessError
from app.models import Setting, UserRole, utc_now
from app.yandex_disk import YandexDisk, YandexDiskError, safe_disk_read_path, safe_path


@pytest.mark.parametrize('path', ['disk:/', 'disk:/Гранд/20 октября/Все фото', 'disk:/Hotel (1)/name #2.jpg'])
def test_private_disk_reads_accept_names_without_expanding_writes(path):
    assert safe_disk_read_path(path) == path
    with pytest.raises(ValueError):
        safe_path(path)


@pytest.mark.parametrize('path', ['https://disk.yandex.ru/client/disk', 'disk:/../secret', 'disk:/a//b',
                                  'disk:/a/./b', 'disk:/a\\b', 'disk:/a\x00b', 'app:/Other'])
def test_external_read_path_rejects_urls_traversal_and_controls(path):
    with pytest.raises(ValueError):
        safe_disk_read_path(path)


def test_external_directory_reads_all_pages_and_cannot_write_outside_app():
    async def run():
        disk = YandexDisk('synthetic-token')
        disk._json = AsyncMock(side_effect=[
            {'type': 'dir', '_embedded': {'items': [{'name': 'a.jpg', 'type': 'file'}], 'total': 2}},
            {'type': 'dir', '_embedded': {'items': [{'name': 'b.jpg', 'type': 'file'}], 'total': 2}},
        ])
        assert len(await disk.disk_list_directory('disk:/Hotel/Все фото', page_size=1)) == 2
        assert [call.kwargs['params']['offset'] for call in disk._json.await_args_list] == [0, 1]
        assert all(call.args[0] == 'GET' for call in disk._json.await_args_list)
        with pytest.raises(ValueError):
            await disk.upload_bytes('disk:/Hotel/replace.jpg', b'unchanged')
        with pytest.raises(ValueError):
            await disk.delete('disk:/Hotel/replace.jpg')
        assert disk._json.await_count == 2
    asyncio.run(run())


def png(color):
    buffer = io.BytesIO()
    Image.new('RGB', (120, 140), color).save(buffer, 'PNG')
    return buffer.getvalue()


class ReadDisk:
    token = 'synthetic-only'

    def __init__(self, *, broken=False):
        raw = png('red')
        self.raw = {'disk:/Hotel/a.jpg': raw, 'disk:/Hotel/Family/copy.jpg': raw,
                    'disk:/Hotel/Family/b.jpg': b'broken' if broken else png('blue')}
        self.folders = {
            'disk:/Hotel': [{'type': 'dir', 'name': 'Family', 'path': 'disk:/Hotel/Family'},
                            {'type': 'file', 'name': 'notes.txt', 'path': 'disk:/Hotel/notes.txt'}],
            'disk:/Hotel/Family': [],
        }
        for path, payload in self.raw.items():
            parent, name = path.rsplit('/', 1)
            self.folders[parent].append({'type': 'file', 'name': name, 'path': path,
                'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()})
        self.disk_list_directory = AsyncMock(side_effect=lambda path: self.folders[path])
        self.disk_download_bytes = AsyncMock(side_effect=lambda path: self.raw[path])
        self.upload_bytes = AsyncMock()
        self.delete = AsyncMock()


async def setup(monkeypatch, disk):
    monkeypatch.setattr('app.disk_folder_audit.read_storage', lambda: disk)
    monkeypatch.setattr('app.disk_folder_audit.config', SimpleNamespace(openai_api_key=''))
    engine, factory = await fixture()
    async with factory() as session:
        session.add(UserRole(user_id=1, role='OWNER'))
        await session.commit()
    api = SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda request: request.body))
    feature = DiskFolderAudits(api)
    request = Request({'id': 1, 'roles': ['OWNER']}, {'path': 'disk:/Hotel', 'bookingId': None, 'requestKey': str(uuid4())})
    return engine, factory, feature, request


@pytest.mark.parametrize('broken', [False, True])
def test_recursive_audit_reads_existing_files_and_preserves_sources(monkeypatch, broken):
    async def run():
        disk = ReadDisk(broken=broken)
        original = dict(disk.raw)
        engine, factory, feature, request = await setup(monkeypatch, disk)
        try:
            first = json.loads((await feature.start(request)).text)
            again = json.loads((await feature.start(request)).text)
            assert again['id'] == first['id'] and again['status'] == 'PENDING'
            for _ in range(5):
                await process_disk_audit(engine, storage=disk, batch_size=2)
            status_request = Request(request['miniapp_actor'], cid=first['id'])
            result = json.loads((await feature.status(status_request)).text)
            assert result['status'] == 'COMPLETED' and result['totalPhotos'] == 3
            assert result['summary']['listingComplete'] is True
            assert result['summary']['checked'] == (2 if broken else 3)
            assert result['summary']['complete'] is (not broken)
            assert result['summary']['failed'] == (1 if broken else 0)
            assert result['summary']['uniqueOriginals'] == (1 if broken else 2)
            assert len(result['summary']['exactCopies']) == 1
            assert result['otherFiles'] == 1 and result['processed'] == 3
            assert disk.raw == original
            disk.upload_bytes.assert_not_awaited()
            disk.delete.assert_not_awaited()
            assert result['aiFrames'] == [] and not result['aiConfigured']
            assert 'synthetic-only' not in json.dumps(result)
            async with factory() as session:
                assert len(list(await session.scalars(select(Setting).where(Setting.key.like(PREFIX + '%'))))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_permissions_and_expired_owner_stop_before_disk_access(monkeypatch):
    async def run():
        disk = ReadDisk()
        engine, factory, feature, request = await setup(monkeypatch, disk)
        try:
            forbidden = Request({'id': 2, 'roles': ['PHOTOGRAPHER']}, request.body)
            forbidden.query = {'path': 'disk:/'}
            with pytest.raises(AccessError):
                await feature.folders(forbidden)
            disk.disk_list_directory.assert_not_awaited()
            await feature.start(request)
            async with factory() as session:
                role = await session.scalar(select(UserRole).where(UserRole.user_id == 1, UserRole.role == 'OWNER'))
                await session.delete(role)
                await session.commit()
            assert await process_disk_audit(engine, storage=disk)
            disk.disk_list_directory.assert_not_awaited()
            result = json.loads((await feature.status(Request(request['miniapp_actor'], cid=request.body['requestKey']))).text)
            assert result['status'] == 'FAILED' and not result['summary']['complete']
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_provider_scope_failure_is_explicit_and_never_empty_success(monkeypatch):
    async def run():
        disk = ReadDisk()
        disk.disk_list_directory = AsyncMock(side_effect=YandexDiskError('Yandex.Disk HTTP 403: ForbiddenError'))
        engine, _, feature, request = await setup(monkeypatch, disk)
        try:
            request.query = {'path': 'disk:/'}
            with pytest.raises(AccessError, match='не разрешил чтение') as error:
                await feature.folders(request)
            assert error.value.status == 403
            await feature.start(request)
            await process_disk_audit(engine, storage=disk)
            result = json.loads((await feature.status(Request(request['miniapp_actor'], cid=request.body['requestKey']))).text)
            assert result['status'] == 'FAILED'
            assert result['summary']['listingComplete'] is False and result['summary']['complete'] is False
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_stale_worker_resumes_but_account_change_stops_reading(monkeypatch):
    async def run():
        disk = ReadDisk()
        engine, factory, feature, request = await setup(monkeypatch, disk)
        try:
            await feature.start(request)
            key = PREFIX + request.body['requestKey']
            async with factory() as session:
                row = await session.get(Setting, key)
                job = json.loads(row.value)
                job.update(status='RUNNING', claimedAt=(utc_now() - timedelta(minutes=5)).isoformat(), claim='abandoned')
                row.value = json.dumps(job)
                await session.commit()
            assert await process_disk_audit(engine, storage=disk)
            assert disk.disk_list_directory.await_count == 1
            disk.token = 'different-account-token'
            assert await process_disk_audit(engine, storage=disk)
            assert disk.disk_list_directory.await_count == 1
            async with factory() as session:
                job = json.loads((await session.get(Setting, key)).value)
                assert job['status'] == 'FAILED'
                assert public_job(job)['summary']['listingComplete'] is False
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_manifest_limit_is_failure_not_full_audit(monkeypatch):
    async def run():
        disk = ReadDisk()
        engine, _, feature, request = await setup(monkeypatch, disk)
        monkeypatch.setattr('app.disk_folder_audit.MAX_PHOTOS', 1)
        try:
            await feature.start(request)
            for _ in range(3):
                await process_disk_audit(engine, storage=disk)
            result = json.loads((await feature.status(Request(request['miniapp_actor'], cid=request.body['requestKey']))).text)
            assert result['status'] == 'FAILED' and not result['summary']['listingComplete']
            assert not result['summary']['complete']
            disk.disk_download_bytes.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_ai_results_are_separate_from_deterministic_file_checks(monkeypatch):
    async def run():
        disk = ReadDisk()
        engine, _, feature, request = await setup(monkeypatch, disk)
        monkeypatch.setattr('app.disk_folder_audit.config', SimpleNamespace(openai_api_key='synthetic-test-only'))
        analyze = AsyncMock(return_value={'status': 'unavailable'})
        monkeypatch.setattr('app.services.development_ai.analyze_batch', analyze)
        try:
            await feature.start(request)
            for _ in range(3):
                await process_disk_audit(engine, storage=disk)
            result = json.loads((await feature.status(Request(request['miniapp_actor'], cid=request.body['requestKey']))).text)
            assert result['summary']['complete'] is True and result['summary']['checked'] == 3
            assert result['aiFrames'] == [] and len(result['aiErrors']) == 1
            assert result['aiErrors'][0]['photos'] == [1, 2, 3]
            assert analyze.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_repeated_worker_interruptions_end_with_visible_failure(monkeypatch):
    async def run():
        disk = ReadDisk()
        engine, factory, feature, request = await setup(monkeypatch, disk)
        try:
            await feature.start(request)
            key = PREFIX + request.body['requestKey']
            async with factory() as session:
                row = await session.get(Setting, key)
                job = json.loads(row.value)
                job.update(status='RUNNING', attempts=5, claimedAt=(utc_now() - timedelta(minutes=5)).isoformat())
                row.value = json.dumps(job)
                await session.commit()
            assert await process_disk_audit(engine, storage=disk)
            disk.disk_list_directory.assert_not_awaited()
            async with factory() as session:
                job = json.loads((await session.get(Setting, key)).value)
                assert job['status'] == 'FAILED' and 'прервалась' in job['error']
        finally:
            await engine.dispose()
    asyncio.run(run())
