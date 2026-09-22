import asyncio
import hashlib
import json
from datetime import date
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from test_workflow_services import booking_data, fixture

from app.miniapp_security import AccessError
from app.models import (
    Booking,
    OperationRequest,
    Package,
    PayrollEntry,
    Receipt,
    Sale,
    SaleDraft,
)
from app.workday import Workday
from app.workflow import Workflow

ACTOR = {'id': 1, 'roles': ['MANAGER']}


async def setup():
    engine, factory = await fixture()
    async def rows(conn, sql, **params):
        return list((await conn.execute(text(sql.replace(' FOR UPDATE', '')), params)).mappings())
    api = SimpleNamespace(engine=engine, rows=rows, day=date.fromisoformat,
                          today=lambda: date(2026, 9, 22), tz=ZoneInfo('Europe/Moscow'))
    return engine, factory, Workday(api)


def packet(key, kind, data):
    return {'actorId': 1, 'key': f'workflow-request-{key}', 'kind': kind, 'date': '2026-09-22', 'data': data}


def test_booking_replay_is_atomic_and_rechecks_actor():
    async def run():
        engine, factory, work = await setup()
        try:
            body = packet('booking', 'booking', booking_data() | {'photographer_id': None})
            first = json.loads((await work.execute(ACTOR, body)).text)
            again = json.loads((await work.execute(ACTOR, body)).text)
            assert first == again
            async with factory() as session:
                assert await session.scalar(select(func.count(Booking.id))) == 1
                assert await session.scalar(select(func.count(OperationRequest.id))) == 1
            with pytest.raises(AccessError) as err:
                await work.execute(ACTOR, body | {'actorId': 2})
            assert err.value.status == 403
            with pytest.raises(AccessError) as err:
                await work.execute(ACTOR, body | {'data': body['data'] | {'room': 'other'}})
            assert err.value.status == 409
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_sale_with_media_replays_once_and_rejects_changed_price(monkeypatch):
    uploaded = []
    async def telegram(self, actor, raw, **kwargs):
        digest = hashlib.sha256(raw).hexdigest()
        uploaded.append(digest)
        return digest, digest
    async def disk(self, draft, raw):
        return 'app:/selected/' + hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(Workflow, 'save_telegram', telegram)
    monkeypatch.setattr(Workflow, 'save_selected', disk)

    async def run():
        engine, factory, work = await setup()
        try:
            b = json.loads((await work.execute(ACTOR, packet('book', 'booking', booking_data() | {'photographer_id': None}))).text)
            async with factory() as session:
                booking = await session.get(Booking, b['bookingId'])
                booking.status = 'READY_FOR_SALE'
                booking.photographer_id = 2
                await session.commit()
            bad = packet('bad-price', 'sale_start', {'booking': {'id': b['bookingId']}, 'price': '200.00'})
            with pytest.raises(AccessError) as err:
                await work.execute(ACTOR, bad)
            assert err.value.status == 409
            async with factory() as session:
                assert await session.scalar(select(func.count(SaleDraft.id))) == 0
                assert await session.scalar(select(func.count(OperationRequest.id))) == 1
            start = packet('start', 'sale_start', {'booking': {'id': b['bookingId']}, 'price': '400.00'})
            await work.execute(ACTOR, start)
            draft = {'key': start['key']}
            operations = [
                (packet('receipt', 'sale_receipt', {'draft': draft}), b'\xff\xd8\xff' + b'receipt' * 40),
                (packet('counts', 'sale_counts', {'draft': draft, 'total': 150, 'sold': 1, 'price': '400.00'}), None),
                (packet('selected', 'sale_selected', {'draft': draft}), b'\xff\xd8\xff' + b'photo' * 40),
                (packet('complete', 'sale_complete', {'draft': draft, 'price': '400.00'}), None),
            ]
            for body, raw in operations:
                first = json.loads((await work.execute(ACTOR, body, raw)).text)
                assert first == json.loads((await work.execute(ACTOR, body, raw)).text)
            async with factory() as session:
                assert await session.scalar(select(func.count(Sale.id))) == 1
                sale = await session.scalar(select(Sale))
                assert sale.amount == 400 and sale.percent == 0 and sale.payment_status == 'UNPAID'
                assert await session.scalar(select(func.count(Receipt.id))) == 1
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                (await session.get(Package, 1)).price_per_photo = 500
                await session.commit()
            assert len(uploaded) == 2
            # Server acknowledgements win over later price changes on an exact replay.
            assert json.loads((await work.execute(ACTOR, operations[-1][0])).text)['saleId'] == sale.id
            with pytest.raises(AccessError) as err:
                await work.execute(ACTOR, operations[0][0], b'\xff\xd8\xff' + b'changed' * 40)
            assert err.value.status == 409
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_missing_dependency_and_revoked_role_never_create_sale():
    async def run():
        engine, factory, work = await setup()
        try:
            body = packet('finish', 'sale_complete', {'draft': {'key': 'missing-key'}, 'price': '400.00'})
            with pytest.raises(AccessError) as err:
                await work.execute(ACTOR, body)
            assert err.value.status == 424
            async with engine.begin() as conn:
                await conn.execute(text('DELETE FROM user_roles WHERE user_id=1'))
            with pytest.raises(AccessError) as err:
                await work.execute(ACTOR, packet('book', 'booking', booking_data() | {'photographer_id': None}))
            assert err.value.status == 403
            async with factory() as session:
                assert await session.scalar(select(func.count(Booking.id))) == 0
                assert await session.scalar(select(func.count(Sale.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())
