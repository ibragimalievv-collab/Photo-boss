"""Async provider deletion must preserve honest audit/cleanup outcomes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from sale_gallery_provider_cleanup import (
    DiagnosticCleanupTimeout,
    delete_diagnostic_uuid_directory,
)

UUID = "a" * 32
PATH = "app:/PhotoBoss/system/diagnostics/sale-flow-" + UUID


class ProviderError(RuntimeError):
    def __init__(self, status):
        self.http_status = status


def check(storage, path=PATH, **options):
    return asyncio.run(delete_diagnostic_uuid_directory(storage, path, expected_uuid=UUID,
        provider_error_type=ProviderError, timeout=1, interval=0.001, **options))


def test_async_delete_is_sent_once_then_same_directory_is_polled_until_404():
    storage = SimpleNamespace(delete=AsyncMock(return_value={"status": 202}),
        metadata=AsyncMock(side_effect=[{"type": "dir"}, {"type": "dir"}, ProviderError(404)]))
    result = check(storage)
    assert result["confirmed"] is True and result["metadata_polls"] == 3
    storage.delete.assert_awaited_once_with(PATH)
    assert storage.metadata.await_args_list == [call(PATH)] * 3


def test_already_absent_uuid_directory_is_confirmed_by_structured_404():
    storage = SimpleNamespace(delete=AsyncMock(), metadata=AsyncMock(side_effect=ProviderError(404)))
    assert check(storage)["metadata_polls"] == 1


def test_non_404_metadata_failure_is_preserved_instead_of_claiming_cleanup():
    storage = SimpleNamespace(delete=AsyncMock(), metadata=AsyncMock(side_effect=ProviderError(503)))
    with pytest.raises(ProviderError) as failure:
        check(storage)
    assert failure.value.http_status == 503
    storage.delete.assert_awaited_once_with(PATH)
    storage.metadata.assert_awaited_once_with(PATH)


def test_pending_deletion_has_a_real_deadline_and_never_succeeds_without_404():
    async def run():
        storage = SimpleNamespace(delete=AsyncMock(), metadata=AsyncMock(return_value={"type": "dir"}))
        with pytest.raises(DiagnosticCleanupTimeout, match="deletion_not_confirmed"):
            await delete_diagnostic_uuid_directory(storage, PATH, expected_uuid=UUID,
                provider_error_type=ProviderError, timeout=0.02, interval=0.002)
        storage.delete.assert_awaited_once_with(PATH)
        assert storage.metadata.await_count >= 1
    asyncio.run(run())


def test_deadline_includes_delete_and_cancels_a_stalled_request():
    async def run():
        cancelled = []
        async def stalled_delete(path):
            try:
                await asyncio.sleep(1)
            finally:
                cancelled.append(path)
        storage = SimpleNamespace(delete=stalled_delete, metadata=AsyncMock())
        with pytest.raises(DiagnosticCleanupTimeout):
            await delete_diagnostic_uuid_directory(storage, PATH, expected_uuid=UUID,
                provider_error_type=ProviderError, timeout=0.02, interval=0.002)
        assert cancelled == [PATH]
        storage.metadata.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("path", ["app:/PhotoBoss", PATH + "/child", PATH + "/../other",
                                  "app:/PhotoBoss/system/diagnostics/sale-flow-" + "b" * 32])
def test_every_other_path_is_rejected_before_provider_access(path):
    storage = SimpleNamespace(delete=AsyncMock(), metadata=AsyncMock())
    with pytest.raises(ValueError, match="exact_own_uuid"):
        check(storage, path)
    storage.delete.assert_not_awaited()
    storage.metadata.assert_not_awaited()


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), float("nan"), True])
def test_unbounded_or_invalid_timeout_is_rejected_before_provider_access(timeout):
    storage = SimpleNamespace(delete=AsyncMock(), metadata=AsyncMock())
    with pytest.raises(ValueError, match="timeout_must_be_bounded"):
        asyncio.run(delete_diagnostic_uuid_directory(storage, PATH, expected_uuid=UUID,
            provider_error_type=ProviderError, timeout=timeout))
    storage.delete.assert_not_awaited()
    storage.metadata.assert_not_awaited()
