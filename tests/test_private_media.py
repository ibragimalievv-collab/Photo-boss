"""Private web originals remain readable without a Telegram conversation."""
import asyncio
import hashlib
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto

from app.services import media_files
from app.services.photo_storage import _download
from app.services.receipts import download_receipt
from app.workflow import Workflow
from app.yandex_disk import ROOT, YandexDiskError

RAW = b'\x89PNG\r\n\x1a\n' + b'original-pixels' * 20
PATH = ROOT + '/web-media/7/test.png'


def test_connection_loss_during_json_read_is_retryable_without_handler_failure():
    from app.miniapp_api import MiniApp
    from app.miniapp_security import AccessError
    request = SimpleNamespace(content_length=None, content=SimpleNamespace(
        at_eof=lambda: False, read=AsyncMock(side_effect=ConnectionResetError('Connection lost'))))
    with pytest.raises(AccessError) as error:
        asyncio.run(MiniApp.body(None, request))
    assert error.value.status == 503


def test_fallback_write_is_private_actor_scoped_and_checksum_confirmed(monkeypatch):
    disk = SimpleNamespace(ensure_dir=AsyncMock(), upload_bytes=AsyncMock())
    monkeypatch.setattr(media_files, 'configured_storage', lambda: disk)
    path, unique = asyncio.run(media_files.save_private_media(7, RAW))
    digest = hashlib.sha256(RAW).hexdigest()
    assert path == ROOT + f'/web-media/7/{digest}.png'
    assert unique == 'web-sha256:' + digest
    disk.upload_bytes.assert_awaited_once_with(path, RAW, content_type='image/png')
    disk.upload_bytes.side_effect = YandexDiskError('checksum not confirmed')
    with pytest.raises(YandexDiskError):
        asyncio.run(media_files.save_private_media(7, RAW))


def test_receipts_storage_worker_and_telegram_preview_read_private_original(monkeypatch):
    disk = SimpleNamespace(download_bytes=AsyncMock(return_value=RAW))
    monkeypatch.setattr(media_files, 'configured_storage', lambda: disk)
    bot = SimpleNamespace(download=AsyncMock())
    assert asyncio.run(_download(bot, PATH)) == RAW
    assert asyncio.run(download_receipt(bot, SimpleNamespace(file_id=PATH, file_size=None))) == RAW
    image = asyncio.run(media_files.telegram_media_input(bot, PATH))
    assert image.data == RAW and image.filename.endswith('.png')
    bot.download.assert_not_awaited()
    assert disk.download_bytes.await_args_list[1].kwargs['max_bytes'] == 8 * 1024 * 1024


def test_telegram_legacy_download_is_bounded_and_does_not_change_bytes():
    async def download(_file_id, *, destination, timeout):
        destination.write(RAW)
    bot = SimpleNamespace(download=download)
    assert asyncio.run(media_files.load_private_media(bot, 'legacy-telegram-id')) == RAW
    with pytest.raises(ValueError):
        asyncio.run(media_files.load_private_media(bot, 'legacy-telegram-id', max_bytes=8))
    with pytest.raises(ValueError):
        asyncio.run(media_files.load_private_media(bot, ROOT + '/../private'))


def test_document_receipt_can_be_reviewed_without_photo_conversion():
    error = TelegramBadRequest(method=SendPhoto(chat_id=42, photo='document-id'), message='Bad Request: wrong file identifier')
    message = SimpleNamespace(bot=None, answer_photo=AsyncMock(side_effect=error), answer_document=AsyncMock())
    asyncio.run(media_files.send_private_image(message, 'document-id', caption='Receipt'))
    message.answer_document.assert_awaited_once_with('document-id', caption='Receipt')


def test_invalid_old_analysis_is_displayed_as_unavailable_not_unpaid_or_approved():
    from datetime import UTC, datetime

    from app.workflow import receipt_analysis, receipt_state
    for saved in ('not-json', '[]', 'null'):
        assert receipt_analysis(saved) == {}
        row = receipt_state(SimpleNamespace(id=1, status='PENDING', file_id='saved', analysis=saved,
                                            expected_amount=400, created_at=datetime(2026, 10, 17, tzinfo=UTC)))
        assert row['stored'] is True and row['analysisStatus'] == 'unavailable'
        assert row['status'] == 'PENDING' and row['extractedAmount'] is None


def test_invalid_media_failure_does_not_claim_key_or_block_replacement(monkeypatch):
    from sqlalchemy import func, select
    from test_workflow_replay import ACTOR, packet, setup
    from test_workflow_services import booking_data

    from app.miniapp_security import AccessError
    from app.models import Booking, OperationRequest

    async def run():
        engine, factory, work = await setup()
        monkeypatch.setattr(Workflow, 'save_telegram', AsyncMock(return_value=('receipt', 'receipt-unique')))
        try:
            import json
            booking = json.loads((await work.execute(ACTOR, packet('book', 'booking', booking_data() | {'photographer_id': None}))).text)
            async with factory() as session:
                (await session.get(Booking, booking['bookingId'])).status = 'READY_FOR_SALE'
                await session.commit()
            start = packet('start', 'sale_start', {'booking': {'id': booking['bookingId']}, 'price': '400.00'})
            await work.execute(ACTOR, start)
            body = packet('media-replace', 'sale_receipt', {'draft': {'key': start['key']}})
            with pytest.raises(AccessError) as invalid:
                await work.execute(ACTOR, body, b'empty')
            assert invalid.value.status == 400
            async with factory() as session:
                assert await session.scalar(select(func.count(OperationRequest.id)).where(OperationRequest.request_key == body['key'])) == 0
            # Same key and target, replacement original: no success existed yet.
            await work.execute(ACTOR, body, RAW)
            async with factory() as session:
                assert await session.scalar(select(func.count(OperationRequest.id)).where(OperationRequest.request_key == body['key'])) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


class Request(dict):
    def __init__(self, actor, rid):
        super().__init__(miniapp_actor=actor)
        self.match_info = {'id': str(rid)}


def test_receipt_image_requires_booking_scope_before_reading_storage(monkeypatch):
    from sqlalchemy import select
    from test_sales_followups import prepared, purchase

    from app.miniapp_security import AccessError
    from app.models import Booking, Receipt, Shooting

    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                result = await purchase(session, bid, 1, 2)
                (await session.get(Booking, bid)).status = 'SOLD'
                (await session.scalar(select(Shooting).where(Shooting.booking_id == bid))).full_upload_completed_at = None
                await session.commit()
                receipt = await session.scalar(select(Receipt))
                rid, did = receipt.id, result.draft.id
            loader = AsyncMock(return_value=RAW)
            monkeypatch.setattr(media_files, 'load_private_media', loader)
            workflow = Workflow(SimpleNamespace(engine=engine, bot=None))
            with pytest.raises(AccessError) as denied:
                await workflow.receipt_image(Request({'id': 3, 'roles': ['MANAGER']}, rid))
            assert denied.value.status == 404
            loader.assert_not_awaited()
            response = await workflow.receipt_image(Request({'id': 1, 'roles': ['MANAGER']}, rid))
            assert response.body == RAW and response.headers['Cache-Control'] == 'no-store'
            response = await workflow.draft_receipt_image(Request({'id': 1, 'roles': ['MANAGER']}, did))
            assert response.body == RAW
            listing = Workflow(SimpleNamespace(engine=engine, today=lambda: date(2026, 10, 17)))
            import json
            data = json.loads((await listing.listing({'miniapp_actor': {'id': 2, 'roles': ['PHOTOGRAPHER']}})).text)
            booking = data['bookings'][0]
            assert booking['status'] == 'SOLD'
            assert booking['shootingStatus'] == 'READY_FOR_SALE'
            assert booking['canFullUpload'] is True
            assert booking['receipts'][0]['stored'] is True
            assert booking['receipts'][0]['status'] == 'PENDING'
            assert booking['receipts'][0]['analysisStatus'] != 'extracted'
        finally:
            await engine.dispose()
    asyncio.run(run())
