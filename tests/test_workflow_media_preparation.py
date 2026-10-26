"""Slow providers must not occupy the database pool or bypass final checks."""
import asyncio
import io
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_sales_followups import prepared
from test_workflow_services import booking_data

from app.miniapp_api import MiniApp
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    OperationRequest,
    Receipt,
    Sale,
    SaleDraftPhoto,
    Shooting,
    User,
)
from app.services.sale_workflow import (
    capture_draft_receipt,
    set_sale_counts,
    start_sale_draft,
)
from app.workday import Workday
from app.workflow import Workflow

ACTOR = {'id': 1, 'roles': ['MANAGER']}


def image():
    out = io.BytesIO()
    Image.new('RGB', (30, 30), 'blue').save(out, format='JPEG')
    return out.getvalue()


async def setup(path, kind):
    url = f'sqlite+aiosqlite:///{path}'
    initial, _, bid = await prepared(url)
    await initial.dispose()
    engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_timeout=.2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        actor = await session.get(User, 1)
        if kind.startswith('sale_'):
            draft = await start_sale_draft(session, actor, {'MANAGER'}, bid)
            if kind == 'sale_selected':
                await capture_draft_receipt(session, actor, draft, 'receipt', 'receipt-id', 'receipt-hash', {'status': 'not_configured'})
                await set_sale_counts(session, actor, draft, 10, 1)
            data = {'draft': {'id': draft.id}}
        elif kind == 'booking_receipt':
            (await session.get(Booking, bid)).deposit = 1000
            data = {'booking': {'id': bid}, 'purpose': 'DEPOSIT'}
        elif kind == 'delivery_photo':
            data = {'booking': bid, 'filename': 'original.jpg'}
        else:
            shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
            shooting.full_upload_completed_at = None
            data = {'shooting': {'id': shooting.id}}
        await session.commit()
    api = MiniApp(engine, SimpleNamespace(), [])
    api.today = lambda: date(2026, 9, 22)
    api.day = date.fromisoformat
    original_rows = api.rows

    async def rows(conn, sql, **params):
        return await original_rows(conn, sql.replace(' FOR UPDATE', ''), **params)

    api.rows = rows
    body = {'actorId': 2 if kind in {'shoot_photo', 'delivery_photo'} else 1, 'key': 'media-test-key-000001', 'kind': kind,
            'date': '2026-09-22', 'data': data}
    return engine, factory, Workday(api), body


@pytest.mark.parametrize('kind', ['sale_selected', 'sale_receipt', 'booking_receipt', 'shoot_photo', 'delivery_photo'])
def test_pending_media_releases_only_pool_slot_and_booking_commits(tmp_path, monkeypatch, kind):
    async def run():
        engine, factory, workday, body = await setup(tmp_path / 'pool.sqlite', kind)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def delayed(*args, **kwargs):
            calls.append(kind)
            assert engine.pool.checkedout() == 0, 'Provider upload holds the application database connection'
            entered.set()
            await release.wait()
            return ('app:/private/receipt.jpg', 'private-receipt') if kind.endswith('receipt') else 'app:/confirmed/original.jpg'

        monkeypatch.setattr('app.push_notifications.app_channel', lambda: True)
        monkeypatch.setattr('app.workflow.extract_receipt', AsyncMock(return_value={'status': 'not_configured'}))
        monkeypatch.setattr(Workflow, 'save_selected', delayed)
        monkeypatch.setattr(Workflow, 'save_disk_original', delayed)
        monkeypatch.setattr(Workflow, 'save_telegram', delayed)
        if kind == 'delivery_photo':
            monkeypatch.setattr('app.delivery.configured_from_env', lambda: ('fake-token', 'fake-client'))
            monkeypatch.setattr('app.delivery.YandexDisk.ensure_dir', AsyncMock())
            monkeypatch.setattr('app.delivery.YandexDisk.upload_bytes', delayed)
        actor = ACTOR if kind not in {'shoot_photo', 'delivery_photo'} else {'id': 2, 'roles': ['PHOTOGRAPHER']}
        pending = asyncio.create_task(workday.execute(actor, body, image()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            # Both employees share this one pool slot; even the uploading
            # employee can register a booking while their provider is stalled.
            booking = {'actorId': 1, 'key': 'booking-during-media-0001', 'kind': 'booking',
                       'date': body['date'], 'data': booking_data() | {'photographer_id': None}}
            response = await asyncio.wait_for(workday.execute(ACTOR, booking), 1)
            assert json.loads(response.text)['bookingId'] > 1
            release.set()
            saved = json.loads((await asyncio.wait_for(pending, 3)).text)
            replay = json.loads((await workday.execute(actor, body, image())).text)
            assert saved == replay and calls == [kind]
            async with factory() as session:
                assert await session.scalar(select(func.count(OperationRequest.id))) == 2
                if kind == 'sale_selected':
                    assert await session.scalar(select(func.count(Sale.id))) == 1
                    assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 1
                if kind == 'booking_receipt':
                    receipt = await session.scalar(select(Receipt))
                    assert receipt.status == 'PENDING' and receipt.expected_amount == 1000
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('change,status', [('revoked', 403), ('generation', 409), ('takeover', 403), ('counts', 409)])
def test_final_media_link_rechecks_changes_while_provider_waits(tmp_path, monkeypatch, change, status):
    async def run():
        engine, factory, workday, body = await setup(tmp_path / 'race.sqlite', 'sale_selected')
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            entered.set()
            await release.wait()
            return 'app:/confirmed/original.jpg'

        monkeypatch.setattr(Workflow, 'save_selected', delayed)
        pending = asyncio.create_task(workday.execute(ACTOR, body, image()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with engine.begin() as conn:
                sql = {'revoked': 'UPDATE users SET active=FALSE WHERE id=1',
                       'generation': "INSERT INTO settings(key,value) VALUES ('operations:generation','new-generation')",
                       'takeover': 'UPDATE sale_drafts SET created_by_id=3',
                       'counts': "UPDATE sale_drafts SET status='AWAITING_COUNTS'"}[change]
                await conn.execute(text(sql))
            release.set()
            with pytest.raises(AccessError) as error:
                await pending
            assert error.value.status == status
            async with factory() as session:
                assert await session.scalar(select(func.count(Sale.id))) == 0
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 0
                assert await session.scalar(select(func.count(OperationRequest.id))) == 0
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await engine.dispose()
    asyncio.run(run())


def test_prepared_upload_is_bound_to_original_payload(tmp_path, monkeypatch):
    async def run():
        engine, _, workday, body = await setup(tmp_path / 'identity.sqlite', 'sale_selected')
        workflow = Workflow(workday.api)
        monkeypatch.setattr(Workflow, 'save_selected', AsyncMock(return_value='app:/confirmed/original.jpg'))
        raw = image()
        try:
            prepared_media = await workflow.prepare_media(ACTOR, body['kind'], body['data'], raw)
            async with engine.begin() as conn:
                with pytest.raises(AccessError, match='не соответствует'):
                    await workflow.apply(conn, ACTOR, body['kind'], {'draft': {'id': 999}}, raw,
                                         prepared_media=prepared_media)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_already_completed_sale_acknowledgement_does_not_need_disk(tmp_path, monkeypatch):
    async def run():
        engine, factory, workday, body = await setup(tmp_path / 'completed.sqlite', 'sale_selected')
        monkeypatch.setattr(Workflow, 'save_selected', AsyncMock(return_value='app:/confirmed/original.jpg'))
        try:
            saved = json.loads((await workday.execute(ACTOR, body, image())).text)
            async with factory() as session:
                photo = await session.scalar(select(SaleDraftPhoto))
                photo.storage_path = 'disk-ref:11111111-1111-1111-1111-111111111111'
                await session.commit()
            # The last selected original auto-completes a sale. Its original
            # queued sale_complete must still acknowledge it during a later
            # Disk outage, without re-reading a now unavailable reference.
            lookup = AsyncMock(side_effect=AssertionError('Completed acknowledgement read a Disk reference'))
            monkeypatch.setattr('app.disk_sale_media.reference', lookup)
            complete = body | {'key': 'complete-after-photo-0001', 'kind': 'sale_complete',
                               'data': {'draft': body['data']['draft'], 'price': '400.00'}}
            replay = json.loads((await workday.execute(ACTOR, complete)).text)
            assert replay['duplicate'] and replay['saleId'] == saved['saleId']
            lookup.assert_not_called()
            async with factory() as session:
                assert await session.scalar(select(func.count(Sale.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
