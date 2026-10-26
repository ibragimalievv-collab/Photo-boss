"""A pending mark explains its actual reason rather than only asking to wait."""
import asyncio
import json

import pytest
from sqlalchemy import select
from test_attendance_auto_confirmation import DAY, NOW, prepare, submit
from test_hotel_points_review import jpeg
from test_workflow_replay import ACTOR

from app.miniapp_security import AccessError
from app.models import Notification, ShiftCheckIn


@pytest.mark.parametrize('offline', [False, True])
def test_pending_attendance_notice_includes_evidence_reason(monkeypatch, offline):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '1')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            if offline:
                data = {'purpose': 'start', 'claimedAt': NOW.isoformat(),
                        'latitude': 44.9, 'longitude': 37.3, 'accuracy': 10}
                async with engine.begin() as conn:
                    await service.offline(conn, ACTOR, DAY, data, jpeg())
            else:
                await submit(service, latitude=0)
            async with factory() as session:
                notices = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance')))
                assert len(notices) == 3 and {row.user_id for row in notices} == {1, 3, 4}
                for row in notices:
                    payload = json.loads(row.payload)
                    assert payload['requiresReview'] is True
                    assert ('получена офлайн' if offline else 'за пределами зоны') in payload['reviewReason']
                    assert payload['reviewReason'] in row.text
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_confirmed_offline_preparation_cannot_fallback_to_upload_after_correction(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED', '0')
    monkeypatch.setenv('PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW', '1')

    async def run():
        engine, factory, _work, service = await prepare()
        try:
            await submit(service)
            service.send_photo.reset_mock()
            data = {'purpose': 'start', 'claimedAt': NOW.isoformat(),
                    'latitude': 44.9, 'longitude': 37.3, 'accuracy': 10}
            assert await service.prepare_offline_media(ACTOR, DAY, data, jpeg()) is None
            async with factory() as session:
                (await session.scalar(select(ShiftCheckIn))).status = 'REJECTED'
                await session.commit()
            with pytest.raises(AccessError, match='Состояние отметки изменилось'):
                async with engine.begin() as conn:
                    await service.offline(conn, ACTOR, DAY, data, jpeg(), prepared_file_id=None)
            service.send_photo.assert_not_awaited()
            async with factory() as session:
                assert (await session.scalar(select(ShiftCheckIn))).status == 'REJECTED'
        finally:
            await engine.dispose()

    asyncio.run(run())
