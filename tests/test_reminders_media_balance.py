import asyncio
import hashlib
import io
import json
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select
from test_team import Request
from test_workflow_services import booking_data, fixture

from app import development
from app.models import (
    Notification,
    Photo,
    PhotoStorage,
    Receipt,
    Setting,
    Shooting,
    User,
)
from app.push_notifications import AppPush, eligible
from app.services.booking_reminders import queue_twenty_minute_reminders, scheduled_at
from app.services.bookings import create_booking_record
from app.services.sale_workflow import (
    capture_draft_receipt,
    set_sale_counts,
    start_sale_draft,
)
from app.services.shooting_media_audit import (
    audit_next_files,
    audit_summary,
    inspect_original,
)
from app.workflow import Workflow


def test_twenty_minute_reminder_is_once_and_invalidated_by_assignment_time_or_cancel(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    monkeypatch.setenv('WEB_PUSH_PUBLIC_KEY', 'test')
    monkeypatch.setenv('WEB_PUSH_PRIVATE_KEY', 'test')
    monkeypatch.setenv('WEB_PUSH_SUBJECT', 'mailto:test@example.com')
    moment = datetime(2026, 10, 20, 9, 40, tzinfo=UTC)
    monkeypatch.setattr('app.push_notifications.utc_now', lambda: moment.replace(tzinfo=None))

    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data() | {
                    'shoot_date': '2026-10-20', 'shoot_time': '13:00'})
                booking.photographer_id, booking.status = 2, 'ASSIGNED'
                # The configured fixture uses Moscow: 13:00 local = 10:00 UTC.
                assert scheduled_at(booking) == datetime(2026, 10, 20, 10, tzinfo=UTC)
                assert await queue_twenty_minute_reminders(session, moment - timedelta(seconds=1)) == 0
                assert await queue_twenty_minute_reminders(session, moment) == 1
                assert await queue_twenty_minute_reminders(session, moment + timedelta(seconds=15)) == 0
                notice = await session.scalar(select(Notification).where(Notification.kind == 'booking_reminder'))
                assert notice.user_id == 2
                details = json.loads(notice.payload)['description']
                assert '20.10.2026 в 13:00' in details
                assert 'Hotel' in details and booking.room in details
                await session.commit()
                request = Request({'id': 2}, {})
                feed = json.loads((await AppPush(SimpleNamespace(engine=engine)).status(request)).text)
                assert feed['activeDevices'] == 0
                assert feed['items'][0]['pushState'] == 'NO_DEVICE'
                assert feed['items'][0]['description'] == details
                sub = SimpleNamespace(active=True, user_id=2)
                assert await eligible(session, notice, sub)
                monkeypatch.setattr('app.push_notifications.utc_now', lambda: moment + timedelta(minutes=20))
                assert await eligible(session, notice, sub)  # It stays in the inbox after the start time.
                monkeypatch.setattr('app.push_notifications.utc_now', lambda: moment.replace(tzinfo=None))
                booking.photographer_id = 1
                assert not await eligible(session, notice, sub)
                booking.photographer_id = 2
                booking.shoot_time = time(13, 10)
                assert not await eligible(session, notice, sub)
                assert await queue_twenty_minute_reminders(session, moment + timedelta(minutes=10)) == 1
                booking.status = 'CANCELLED'
                assert not await eligible(session, notice, sub)
                assert await queue_twenty_minute_reminders(session, moment) == 0
                await session.commit()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_draft_balance_refreshes_when_deposit_is_approved_after_counts():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data() | {'deposit': '1000'})
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                draft = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, draft, 'receipt', 'unique', 'hash', {'status': 'unavailable'})
                await set_sale_counts(session, actor, draft, 150, 10)
                assert draft.expected_amount == 4000
                deposit = Receipt(booking_id=booking.id, uploaded_by_id=1, purpose='DEPOSIT',
                    file_id='deposit', file_unique_id='deposit', expected_amount=1000,
                    verified_amount=1000, status='PENDING')
                session.add(deposit)
                await session.commit()
                api = Workflow(SimpleNamespace(engine=engine, today=lambda: date(2026, 10, 20)))
                request = Request({'id': 1, 'roles': ['MANAGER']}, {})
                request.query = {}
                before = json.loads((await api.listing(request)).text)['bookings'][0]
                assert before['draft']['expectedAmount'] == '4000.00'
                deposit.status = 'APPROVED'
                await session.commit()
                after = json.loads((await api.listing(request)).text)['bookings'][0]
                assert after['draft']['amount'] == '4000.00'
                assert after['draft']['expectedAmount'] == '3000.00'
                assert after['paidAmount'] == '1000.00'
                assert after['declaredDeposit'] == '1000.00'
                # Repeated reads do not consume the deposit or change revenue.
                assert after == json.loads((await api.listing(request)).text)['bookings'][0]
        finally:
            await engine.dispose()
    asyncio.run(run())


def jpeg(*, gps=False):
    image = Image.new('RGB', (90, 80), 'white')
    for x in range(45):
        for y in range(80):
            image.putpixel((x, y), (x * 5, y * 3, 20))
    exif = Image.Exif()
    if gps:
        exif[0x8825] = {1: 'N', 2: (44., 58., 30.), 3: 'E', 4: (37., 16., 0.)}
    output = io.BytesIO()
    image.save(output, format='JPEG', exif=exif)
    return output.getvalue()


def test_actual_original_gps_hash_and_exact_copies_are_separate_from_similarity():
    raw = jpeg(gps=True)
    first = inspect_original(raw, 1, hashlib.sha256(raw).hexdigest(), len(raw))
    assert first['location']['status'] == 'embedded'
    assert first['location']['latitude'] == pytest.approx(44.975)
    missing = inspect_original(jpeg(), 3)
    assert missing['location']['status'] == 'missing'
    with pytest.raises(ValueError, match='контрольной'):
        inspect_original(raw, 1, '0' * 64, len(raw))
    with pytest.raises(OSError):
        inspect_original(b'not an image', 4)
    report = audit_summary([first, first | {'photoId': 2}, missing], 4)
    assert report['checked'] == 3 and report['uniqueOriginals'] == 2
    assert report['exactCopies'] == [[1, 2]]
    assert not report['complete']


def test_disk_audit_runs_before_sale_and_without_ai_and_persists_failures(monkeypatch):
    raw = jpeg()
    disk = SimpleNamespace(download_bytes=AsyncMock(return_value=raw))
    monkeypatch.setattr('app.services.media_files.configured_storage', lambda: disk)
    monkeypatch.setattr(development, 'config', SimpleNamespace(openai_api_key=''))

    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                booking = await create_booking_record(session, await session.get(User, 1), booking_data())
                booking.photographer_id = 2
                shooting = await session.scalar(select(Shooting))
                shooting.full_upload_completed_at = datetime(2026, 10, 20, tzinfo=UTC).replace(tzinfo=None)
                for n in range(2):
                    photo = Photo(shooting_id=shooting.id, file_id=f'app:/PhotoBoss/shootings/{shooting.id}/{n}.jpg')
                    session.add(photo)
                    await session.flush()
                    session.add(PhotoStorage(photo_id=photo.id, shooting_id=shooting.id,
                        telegram_file_id=photo.file_id, telegram_unique_id=f'unique-{n}', source_kind='DOCUMENT',
                        status='STORED', disk_path=photo.file_id, sha256=hashlib.sha256(raw).hexdigest(),
                        byte_size=len(raw)))
                await session.commit()
                assert await development.queue_reviews(session) == 1
            assert await audit_next_files(factory, limit=1)
            disk.download_bytes.side_effect = OSError('temporary provider failure')
            assert await audit_next_files(factory, limit=1)
            assert not await audit_next_files(factory)
            async with factory() as session:
                state = await session.get(Setting, 'shoot-audit:1')
                files = json.loads(state.value)
                report = audit_summary(files, 2)
                assert report['checked'] == 1 and report['failed'] == 1 and report['complete']
                assert files[1]['status'] == 'unavailable'
            assert not await development.review_one(factory, SimpleNamespace())
        finally:
            await engine.dispose()
    asyncio.run(run())
