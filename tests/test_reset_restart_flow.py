"""Restart the real staff workflow after an archived reset, through HTTP routes."""
import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_miniapp_release import TOKEN, signed
from test_operational_reset import fixture as populated_fixture

from app.attendance import Attendance, install_attendance
from app.miniapp_api import OPERATIONS_GENERATION_HEADER, install_miniapp
from app.models import (
    Booking,
    OperationRequest,
    Photo,
    PhotoStorage,
    Receipt,
    Sale,
    Shooting,
)
from app.operational_reset import reset
from app.workday import Workday
from app.workflow import KINDS, Workflow
from app.yandex_disk import ROOT as DISK_ROOT

RAW = b'\xff\xd8\xff' + b'fresh-camera-photo' * 40 + b'\xff\xd9'
RECEIPT = b'\xff\xd8\xff' + b'new-sale-receipt' * 40 + b'\xff\xd9'


def test_reset_then_new_attendance_booking_shoot_and_sale_work_end_to_end(monkeypatch):
    async def run():
        engine = await populated_fixture()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock())
        app = web.Application(client_max_size=25 * 1024 * 1024)
        api = install_miniapp(app, engine=engine, bot=bot, lessons=[])
        install_attendance(app, api)
        original_rows = api.rows

        async def rows(conn, sql, **params):
            return await original_rows(conn, sql.replace(' FOR UPDATE', ''), **params)

        api.rows = rows
        monkeypatch.setattr(Attendance, 'send_photo', AsyncMock(return_value='new-attendance-photo'))

        async def telegram(_self, _actor, raw, **_kwargs):
            digest = hashlib.sha256(raw).hexdigest()
            return 'fixture:' + digest, digest

        async def selected(_self, draft, raw):
            return f'{DISK_ROOT}/sales/draft-{draft.id}/selected/{hashlib.sha256(raw).hexdigest()}.jpg'

        monkeypatch.setattr(Workflow, 'save_telegram', telegram)
        monkeypatch.setattr(Workflow, 'save_selected', selected)
        monkeypatch.setattr('app.workflow.extract_receipt', AsyncMock(return_value={'status': 'not_configured'}))
        client = TestClient(TestServer(app))
        generation = ''
        day = api.today().isoformat()

        def packet(key, kind, data, actor=1, *, epoch=None):
            body = {'actorId': actor, 'key': 'restart-proof-' + key, 'kind': kind, 'date': day, 'data': data}
            if epoch is not None:
                body['generation'] = epoch
            return body

        def headers(actor=1, *, fresh=True):
            value = {'X-Telegram-Init-Data': signed(8000000000 + actor)}
            if fresh:
                value[OPERATIONS_GENERATION_HEADER] = generation
            return value

        async def request(path, *, actor=1, body=None, fresh=True, expected=200):
            method = client.get if body is None else client.post
            response = await method('/api/miniapp' + path, headers=headers(actor, fresh=fresh), **({'json': body} if body is not None else {}))
            result = await response.json()
            assert response.status == expected, (path, response.status, result)
            return result

        async def operation(body, raw=None, *, expected=200):
            if raw is None:
                return await request('/operations/sync', actor=body['actorId'], body=body, expected=expected)
            media = FormData()
            media.add_field('operation', json.dumps(body))
            media.add_field('file', raw, filename='fixture.jpg', content_type='image/jpeg')
            response = await client.post('/api/miniapp/operations/media', headers=headers(body['actorId']), data=media)
            result = await response.json()
            assert response.status == expected, (body['kind'], response.status, result)
            return result

        booking_fields = {'client_name': 'New guest', 'client_phone': None, 'hotel_id': 1,
            'package_id': 1, 'room': 'New room', 'guest_count': 2, 'deposit': '0',
            'shoot_date': day, 'shoot_time': '15:00', 'photographer_id': None}
        try:
            # Preserve a genuine old acknowledgement, then perform the real
            # typed archive/deletion rather than modelling it with table DELETEs.
            old = packet('old-acknowledged-booking', 'booking', booking_fields)
            assert json.loads((await Workday(api).execute({'id': 1, 'roles': ['OWNER']}, old)).text)['bookingId']
            before_requests = None
            async with factory() as session:
                before_requests = await session.scalar(select(func.count(OperationRequest.id)))
            cleared = await reset(engine, 'reset-request-0001', 1)
            await client.start_server()
            identity = await request('/me')
            generation = identity['operationsGeneration']
            assert generation == cleared['generation'] and generation
            async with factory() as session:
                assert await session.scalar(select(func.count(Booking.id))) == 0
                assert await session.scalar(select(func.count(OperationRequest.id))) == before_requests

            # Stale packets cannot create work or even return an old success.
            rejected = await operation(old, expected=409)
            assert rejected['code'] == 'operations_reset'
            for kind in sorted(KINDS | {'attendance', 'checklist', 'shift_report'}):
                stale = packet('old-' + kind, kind, {}, actor=2, epoch='before-reset')
                rejected = await operation(stale, expected=409)
                assert rejected['operationsGeneration'] == generation
            rejected = await request('/attendance/location', actor=2, body={}, fresh=False, expected=409)
            assert rejected['code'] == 'operations_reset'

            # Staff can mark a fresh shift again, with real validation and the
            # normal server confirmation, using a stub only for Telegram upload.
            image = 'data:image/jpeg;base64,' + base64.b64encode(RAW).decode()
            for purpose, state in [('start', 'STARTED'), ('end', 'FINISHED')]:
                await request('/attendance/location', actor=2, body={'purpose': purpose, 'date': day,
                    'latitude': 41.5, 'longitude': 48.1, 'accuracy': 10})
                attendance = await request('/attendance/photo', actor=2, body={'purpose': purpose, 'date': day, 'image': image})
                assert attendance[purpose]['status'] == state

            fresh = packet('new-booking', 'booking', booking_fields, epoch=generation)
            booking = await operation(fresh)
            assert await operation(fresh) == booking
            bid = booking['bookingId']
            await request(f'/bookings/{bid}/photographer', body={'photographerId': 2, 'previousPhotographerId': None})
            for action in ['pickup', 'start', 'finish', 'ready']:
                stage = await request(f'/bookings/{bid}/shooting/{action}', actor=2, body={})
            assert stage['status'] == 'READY_FOR_SALE'
            sid = stage['shootingId']
            photo = await operation(packet('new-original', 'shoot_photo', {'shooting': {'id': sid}}, actor=2, epoch=generation), RAW)
            async with factory() as session:
                stored = await session.scalar(select(PhotoStorage).where(PhotoStorage.photo_id == photo['photoId']))
                # Fake only the storage worker's external confirmation receipt;
                # close-upload executes its normal count/hash/path validation.
                stored.status = 'STORED'
                stored.disk_path = f'{DISK_ROOT}/shootings/{sid}/fresh.jpg'
                stored.stored_at = datetime.now(UTC).replace(tzinfo=None)
                stored.byte_size = len(RAW)
                stored.sha256 = hashlib.sha256(RAW).hexdigest()
                await session.commit()
            complete = await operation(packet('new-complete-shoot', 'shoot_complete', {'shooting': {'id': sid}}, actor=2, epoch=generation))
            assert complete['photos'] == 1 and complete['percent'] == '10'
            started = await operation(packet('new-sale-start', 'sale_start', {'booking': {'id': bid}, 'price': '400.00'}, actor=2, epoch=generation))
            draft = {'id': started['draftId']}
            await operation(packet('new-sale-receipt', 'sale_receipt', {'draft': draft}, actor=2, epoch=generation), RECEIPT)
            counts = await operation(packet('new-sale-counts', 'sale_counts', {'draft': draft, 'total': 1, 'sold': 1, 'price': '400.00'}, actor=2, epoch=generation))
            assert counts['amount'] == '400.00'
            await operation(packet('new-sale-selected', 'sale_selected', {'draft': draft}, actor=2, epoch=generation), RAW)
            closing = packet('new-sale-complete', 'sale_complete', {'draft': draft, 'price': '400.00'}, actor=2, epoch=generation)
            sold = await operation(closing)
            assert sold['soldPhotos'] == 1 and sold['amount'] == '400.00' and sold['paymentStatus'] == 'UNPAID'
            assert await operation(closing) == sold
            workflow = await request('/workflow', actor=2)
            visible = next(item for item in workflow['bookings'] if item['id'] == bid)
            assert visible['soldTotal'] == 1 and visible['salesAmount'] == '400.00'
            async with factory() as session:
                assert await session.scalar(select(func.count(Booking.id))) == 1
                assert await session.scalar(select(func.count(Photo.id))) == 1
                assert await session.scalar(select(func.count(Receipt.id))) == 1
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert (await session.get(Shooting, sid)).full_upload_completed_at is not None
                sale = await session.get(Sale, sold['saleId'])
                assert sale.amount == 400 and sale.sold_photos == 1 and sale.percent == 10 and sale.commission == 40
                assert await session.scalar(text('SELECT count(*) FROM operational_reset_archives')) == 1
        finally:
            await client.close()
            await engine.dispose()
    asyncio.run(run())
