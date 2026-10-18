"""Existing drafts recover without losing uploaded files or duplicating commissions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from test_workflow_services import booking_data, fixture

from app.handlers import sales
from app.models import SaleDraftPhoto, User
from app.services.bookings import create_booking_record
from app.services.sale_workflow import (
    capture_draft_receipt,
    set_sale_counts,
    start_sale_draft,
)


def test_sixteen_frame_sale_resumes_after_only_four_files_were_stored(monkeypatch):
    import hashlib
    import json

    import pytest
    from test_workflow_replay import ACTOR, packet, setup

    from app.miniapp_security import AccessError
    from app.models import Booking, Receipt, Sale, SaleDraft
    from app.workflow import Workflow

    async def telegram(self, actor, raw, **kwargs):
        digest = hashlib.sha256(raw).hexdigest()
        return digest, digest
    async def disk(self, draft, raw):
        return 'app:/selected/' + hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(Workflow, 'save_telegram', telegram)
    monkeypatch.setattr(Workflow, 'save_selected', disk)

    async def run():
        engine, factory, work = await setup()
        try:
            created = json.loads((await work.execute(ACTOR, packet('16-book', 'booking',
                booking_data() | {'photographer_id': None}))).text)
            async with factory() as session:
                booking = await session.get(Booking, created['bookingId'])
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                await session.commit()
            start = packet('16-start', 'sale_start', {'booking': {'id': created['bookingId']}, 'price': '400.00'})
            await work.execute(ACTOR, start)
            draft = {'key': start['key']}
            await work.execute(ACTOR, packet('16-receipt', 'sale_receipt', {'draft': draft}), b'\xff\xd8\xff'+b'receipt'*40)
            await work.execute(ACTOR, packet('16-counts', 'sale_counts', {'draft': draft, 'total': 156, 'sold': 16, 'price': '400.00'}))
            for n in range(4):
                await work.execute(ACTOR, packet(f'16-photo-{n}', 'sale_selected', {'draft': draft}), b'\xff\xd8\xff'+bytes([n])*200)
            finish = packet('16-complete', 'sale_complete', {'draft': draft, 'price': '400.00'})
            with pytest.raises(AccessError, match='4/16'):
                await work.execute(ACTOR, finish)
            async with factory() as session:
                assert await session.scalar(select(func.count(Sale.id))) == 0
                assert (await session.scalar(select(SaleDraft))).receipt_file_id
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 4
            for n in range(4, 16):
                await work.execute(ACTOR, packet(f'16-photo-{n}', 'sale_selected', {'draft': draft}), b'\xff\xd8\xff'+bytes([n])*200)
            result = json.loads((await work.execute(ACTOR, finish)).text)
            assert result['amount'] == '6400.00' and result['soldPhotos'] == 16
            assert json.loads((await work.execute(ACTOR, finish)).text) == result
            async with factory() as session:
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(Receipt.id))) == 1
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 16
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_album_upload_rechecks_capacity_after_disk_write(monkeypatch):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data())
                booking.photographer_id = 2
                booking.status = 'READY_FOR_SALE'
                draft = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, draft, 'receipt', 'receipt', 'receipt', {'status': 'unavailable'})
                await set_sale_counts(session, actor, draft, 150, 1)
                await session.commit()
                did = draft.id
            # Another album item finishes while this item is uploading to Disk.
            async def disk_write(*args, **kwargs):
                async with factory() as other:
                    other.add(SaleDraftPhoto(draft_id=did, telegram_file_id='other', telegram_unique_id='other',
                        storage_path='app:/sales/other.jpg', sha256='other', byte_size=100))
                    await other.commit()
            storage = SimpleNamespace(ensure_dir=AsyncMock(), upload_bytes=AsyncMock(side_effect=disk_write))
            monkeypatch.setattr(sales, 'Session', factory)
            monkeypatch.setattr(sales, 'selected_storage', AsyncMock(return_value=storage))
            payload = b'\xff\xd8\xff' + b'camera-original' * 20
            async def download(*args, destination, **kwargs):
                destination.write(payload)
            message = SimpleNamespace(photo=None, document=SimpleNamespace(file_size=len(payload),
                mime_type='image/jpeg', file_id='new-file', file_unique_id='new-unique'),
                bot=SimpleNamespace(download=AsyncMock(side_effect=download)), answer=AsyncMock())
            state = SimpleNamespace(get_data=AsyncMock(return_value={'sale_draft_id': did}), clear=AsyncMock())
            await sales.selected_photo(message, state, actor)
            async with factory() as session:
                assert await session.scalar(select(func.count(SaleDraftPhoto.id)).where(SaleDraftPhoto.draft_id == did)) == 1
            assert 'Все выбранные фотографии уже загружены' in message.answer.await_args.args[0]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_count_correction_preserves_receipt_and_uploaded_photos():
    import pytest

    from app.models import Sale, SaleDraft
    from app.services.sale_workflow import complete_sale

    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data())
                booking.photographer_id = 2
                booking.status = 'READY_FOR_SALE'
                draft = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, draft, 'receipt', 'receipt', 'receipt', {'status': 'unavailable'})
                await set_sale_counts(session, actor, draft, 150, 3)
                session.add_all(SaleDraftPhoto(draft_id=draft.id, telegram_file_id=f'file-{n}',
                    telegram_unique_id=f'unique-{n}', storage_path=f'app:/sales/photo-{n}.jpg', sha256=f'hash-{n}', byte_size=100)
                    for n in range(2))
                await session.flush()
                with pytest.raises(ValueError, match='не может быть меньше'):
                    await set_sale_counts(session, actor, draft, 150, 1)
                assert draft.sold_photos == 3
                assert await set_sale_counts(session, actor, draft, 150, 2) == 800
                assert draft.receipt_file_id == 'receipt'
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 2
                result = await complete_sale(session, actor, draft.id)
                assert result.sale.amount == 800 and result.sale.sold_photos == 2
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(SaleDraft.id))) == 1
                with pytest.raises(ValueError, match='уже закрыт'):
                    await set_sale_counts(session, actor, draft, 150, 3)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_workflow_correction_is_scoped_and_completes_the_same_draft():
    import json

    import pytest
    from test_workflow_replay import ACTOR, packet, setup

    from app.miniapp_security import AccessError
    from app.models import Booking, Sale, SaleDraft

    async def run():
        engine, factory, work = await setup()
        try:
            created = json.loads((await work.execute(ACTOR, packet('correction-booking', 'booking',
                booking_data() | {'photographer_id': None}))).text)
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await session.get(Booking, created['bookingId'])
                booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
                draft = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, draft, 'receipt', 'receipt', 'receipt', {'status': 'unavailable'})
                await set_sale_counts(session, actor, draft, 150, 3)
                session.add_all(SaleDraftPhoto(draft_id=draft.id, telegram_file_id=f'file-{n}',
                    telegram_unique_id=f'unique-{n}', storage_path=f'app:/sales/photo-{n}.jpg', sha256=f'hash-{n}', byte_size=100)
                    for n in range(2))
                await session.commit()
                did = draft.id
            correction = packet('correction-counts', 'sale_counts',
                {'draft': {'id': did}, 'total': 150, 'sold': 2, 'price': '400.00'})
            with pytest.raises(AccessError) as error:
                await work.execute({'id': 2, 'roles': ['PHOTOGRAPHER']}, correction | {'actorId': 2})
            assert error.value.status == 403
            first = json.loads((await work.execute(ACTOR, correction)).text)
            assert first['amount'] == '800.00'
            assert json.loads((await work.execute(ACTOR, correction)).text) == first
            complete = packet('correction-complete', 'sale_complete', {'draft': {'id': did}, 'price': '400.00'})
            sale = json.loads((await work.execute(ACTOR, complete)).text)
            assert sale['amount'] == '800.00'
            assert json.loads((await work.execute(ACTOR, complete)).text) == sale
            async with factory() as session:
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(SaleDraft.id))) == 1
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_finalized_original_count_can_correct_an_older_draft_total():
    from app.models import Photo, Shooting, utc_now
    from app.services.sale_workflow import complete_sale

    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data())
                booking.photographer_id = 2
                booking.status = 'READY_FOR_SALE'
                draft = await start_sale_draft(session, actor, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, actor, draft, 'receipt', 'receipt', 'receipt', {'status': 'unavailable'})
                await set_sale_counts(session, actor, draft, 150, 1)
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == booking.id))
                shooting.full_upload_completed_at = utc_now()
                session.add(Photo(shooting_id=shooting.id, file_id='original'))
                session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id='file', telegram_unique_id='unique',
                    storage_path='app:/sales/photo.jpg', sha256='hash', byte_size=100))
                await session.flush()
                await set_sale_counts(session, actor, draft, 1, 1)
                result = await complete_sale(session, actor, draft.id)
                assert result.sale.declared_photo_count == 1 and result.sale.amount == 400
        finally:
            await engine.dispose()
    asyncio.run(run())
