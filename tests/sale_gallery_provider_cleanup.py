"""Bounded confirmation of deletion of this diagnostic's private UUID folder."""

import asyncio
import math
import re
import time

_UUID = re.compile(r"[0-9a-f]{32}")
_PREFIX = "app:/PhotoBoss/system/diagnostics/sale-flow-"


class DiagnosticCleanupTimeout(TimeoutError):
    """The provider has not confirmed absence before the cleanup deadline."""


async def delete_diagnostic_uuid_directory(storage, path, *, expected_uuid,
                                            provider_error_type, timeout=20.0, interval=0.5):
    """One DELETE, then metadata reads of exactly this folder until HTTP 404.

    The deadline includes DELETE and every metadata call. A pending operation,
    timeout, or non-404 provider error is never reported as successful cleanup.
    No operation URLs are followed and no other folders are read or deleted.
    """
    if (not isinstance(expected_uuid, str) or not _UUID.fullmatch(expected_uuid)
            or path != _PREFIX + expected_uuid):
        raise ValueError("cleanup_requires_exact_own_uuid_directory")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 30):
        raise ValueError("cleanup_timeout_must_be_bounded")
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or not 0 < interval <= 5):
        raise ValueError("cleanup_poll_interval_must_be_bounded")
    started = time.monotonic()
    deadline = started + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise DiagnosticCleanupTimeout("provider_uuid_directory_deletion_not_confirmed")
        return value

    polls = 0
    try:
        budget = remaining()
        await asyncio.wait_for(storage.delete(path), timeout=budget)
        while True:
            budget = remaining()
            polls += 1
            try:
                await asyncio.wait_for(storage.metadata(path), timeout=budget)
            except provider_error_type as exc:
                if getattr(exc, "http_status", None) == 404:
                    return {"confirmed": True, "metadata_polls": polls,
                            "elapsed_s": round(time.monotonic() - started, 3)}
                raise
            await asyncio.sleep(min(interval, remaining()))
    except TimeoutError:
        raise DiagnosticCleanupTimeout("provider_uuid_directory_deletion_not_confirmed") from None
