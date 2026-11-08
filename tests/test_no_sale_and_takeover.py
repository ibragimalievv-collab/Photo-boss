"""«Гость не купил», administrator takeover and the home reminder for shoots without an outcome."""
import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace
from typing import ClassVar

import pytest
from sqlalchemy import select
from test_sales_followups import prepared, purchase

from app.miniapp_api import MiniApp
from app.miniapp_security import AccessError
from app.models import (
    AuditLog,
    Booking,
    HotelEmployee,
    Receipt,
    SaleDraft,
    Shooting,
    User,
    UserRole,
    utc_now,
)
from app.no_sale import REASONS, mark, unmark
from app.services.sale_workflow import capture_draft_receipt, start_sale_draft


async def _roles(session, uid):
    return set(await session.scalars(select(UserRole.role).where(UserRole.user_id == uid)))


async def _setup(tmp_path, name):
    url = f"sqlite+aiosqlite:///{tmp_path / name}"
    engine, factory, bid = await prepared(url)
    async with factory() as session:
        session.add_all([User(id=4, tg_id=4, name='Owner'), UserRole(user_id=4, role='OWNER'),
                         User(id=5, tg_id=5, name='Hotel admin'), UserRole(user_id=5, role='ADMIN'),
                         HotelEmployee(user_id=5, hotel_id=1),
                         User(id=6, tg_id=6, name='Other admin'), UserRole(user_id=6, role='ADMIN')])
        await session.commit()
    return engine, factory, bid


def test_photographer_marks_no_sale_and_a_later_sale_clears_it(tmp_path):
    async def run():
        engine, factory, bid = await _setup(tmp_path, 'mark.sqlite')
        try:
            async with factory() as session:
                photographer = await session.get(User, 2)
                result = await mark(session, photographer, await _roles(session, 2), bid, 'PRICE', '  дорого   для   семьи ')
                await session.commit()
                assert result['reason'] == 'PRICE' and result['label'] == REASONS['PRICE']
                assert result['note'] == 'дорого для семьи'
            async with factory() as session:
                booking = await session.get(Booking, bid)
                assert booking.status == 'READY_FOR_SALE', 'the guest can still come back and buy'
                assert booking.no_sale_reason == 'PRICE' and booking.no_sale_by_id == 2 and booking.no_sale_at
                logged = await session.scalar(select(AuditLog).where(AuditLog.action == 'booking_no_sale'))
                assert json.loads(logged.details)['reason'] == 'PRICE'
                # The guest returns: a completed sale clears the mark.
                await purchase(session, bid, 1, 1)
                await session.commit()
            async with factory() as session:
                booking = await session.get(Booking, bid)
                assert booking.status == 'SOLD' and booking.no_sale_at is None and booking.no_sale_reason is None
                cleared = await session.scalar(select(AuditLog).where(AuditLog.action == 'booking_no_sale_cleared'))
                assert json.loads(cleared.details)['by_sale'] is True
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_no_sale_validation_and_access(tmp_path):
    async def run():
        engine, factory, bid = await _setup(tmp_path, 'rules.sqlite')
        try:
            async with factory() as session:
                photographer, other = await session.get(User, 2), await session.get(User, 3)
                with pytest.raises(AccessError) as denied:
                    await mark(session, other, await _roles(session, 3), bid, 'PRICE')
                assert denied.value.status == 403
                with pytest.raises(AccessError) as stranger:
                    await mark(session, await session.get(User, 6), await _roles(session, 6), bid, 'PRICE')
                assert stranger.value.status == 403, 'an administrator of another hotel has no access'
                for reason, note in (('UNKNOWN', ''), ('OTHER', '   '), ('PRICE', 'x' * 501)):
                    with pytest.raises(AccessError) as bad:
                        await mark(session, photographer, {'PHOTOGRAPHER'}, bid, reason, note)
                    assert bad.value.status == 400
                assert await mark(session, photographer, {'PHOTOGRAPHER'}, bid, 'OTHER', 'уехал раньше')
                await session.rollback()
                # Not finished yet: no outcome can be recorded.
                (await session.get(Booking, bid)).status = 'SHOOTING'
                await session.flush()
                with pytest.raises(AccessError):
                    await mark(session, photographer, {'PHOTOGRAPHER'}, bid, 'PRICE')
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_money_in_progress_blocks_no_sale(tmp_path):
    async def run():
        engine, factory, bid = await _setup(tmp_path, 'money.sqlite')
        try:
            async with factory() as session:
                manager = await session.get(User, 1)
                draft = await start_sale_draft(session, manager, {'MANAGER'}, bid)
                await capture_draft_receipt(session, manager, draft, 'r', 'r-unique', 'r-hash', {'status': 'not_configured'})
                await session.commit()
            async with factory() as session:
                with pytest.raises(AccessError) as blocked:
                    await mark(session, await session.get(User, 4), {'OWNER'}, bid, 'PRICE')
                assert blocked.value.status == 409 and 'чек' in str(blocked.value)
                assert (await session.get(SaleDraft, draft.id)).status == 'AWAITING_COUNTS'
                (await session.get(SaleDraft, draft.id)).status = 'CANCELLED'
                session.add(Receipt(booking_id=bid, uploaded_by_id=1, purpose='PAYMENT', file_id='f', file_unique_id='u2',
                                    expected_amount=400, status='PENDING'))
                await session.commit()
            async with factory() as session:
                with pytest.raises(AccessError) as receipt:
                    await mark(session, await session.get(User, 4), {'OWNER'}, bid, 'PRICE')
                assert receipt.value.status == 409 and 'чек оплаты' in str(receipt.value)
            async with factory() as session:
                await purchase(session, bid, 2, 1)
                await session.commit()
            async with factory() as session:
                with pytest.raises(AccessError) as sold:
                    await mark(session, await session.get(User, 4), {'OWNER'}, bid, 'PRICE')
                assert sold.value.status in {403, 409}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_no_sale_abandons_only_an_unpaid_draft_of_the_right_person(tmp_path):
    async def run():
        engine, factory, bid = await _setup(tmp_path, 'draft.sqlite')
        try:
            async with factory() as session:
                draft = await start_sale_draft(session, await session.get(User, 1), {'MANAGER'}, bid)
                await session.commit()
            async with factory() as session:
                with pytest.raises(AccessError) as busy:
                    await mark(session, await session.get(User, 2), {'PHOTOGRAPHER'}, bid, 'LATER')
                assert busy.value.status == 409 and 'Manager' in str(busy.value)
            async with factory() as session:
                assert await mark(session, await session.get(User, 5), await _roles(session, 5), bid, 'LATER')
                await session.commit()
            async with factory() as session:
                assert (await session.get(SaleDraft, draft.id)).status == 'CANCELLED'
                assert await session.scalar(select(AuditLog.id).where(AuditLog.action == 'sale_draft_cancelled'))
                cleared = await unmark(session, await session.get(User, 2), {'PHOTOGRAPHER'}, bid)
                await session.commit()
                assert cleared['reason'] == 'LATER'
                assert (await session.get(Booking, bid)).no_sale_at is None
                assert await unmark(session, await session.get(User, 2), {'PHOTOGRAPHER'}, bid) is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def _api(engine):
    api = MiniApp(engine, SimpleNamespace(), [])
    api.today = lambda: date(2026, 9, 22)
    api.day = date.fromisoformat
    return api


def test_home_reminds_about_finished_shoots_without_outcome(tmp_path):
    async def run():
        engine, factory, bid = await _setup(tmp_path, 'home.sqlite')
        try:
            async with factory() as session:
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                shooting.ready_for_sale_at = utc_now() - timedelta(hours=3)
                await session.commit()
            api = _api(engine)
            actor = {'id': 2, 'roles': ['PHOTOGRAPHER']}
            async with engine.connect() as conn:
                waiting = (await api.dashboard_attention(conn, actor, api.today()))['awaitingSale']
            assert [row['id'] for row in waiting] == [bid]
            async with factory() as session:
                await mark(session, await session.get(User, 2), {'PHOTOGRAPHER'}, bid, 'NOT_LIKED')
                await session.commit()
            async with engine.connect() as conn:
                assert (await api.dashboard_attention(conn, actor, api.today()))['awaitingSale'] == []
                listed = await api.bookings_data(conn, actor, date(2026, 9, 22))
            assert listed[0]['noSale']['reason'] == 'NOT_LIKED'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_shoot_control_drops_sale_and_delivery_tasks_after_no_sale(tmp_path):
    async def run():
        engine, factory, bid = await _setup(tmp_path, 'control.sqlite')
        try:
            from app.delivery import Delivery
            api = _api(engine)
            delivery = Delivery.__new__(Delivery)
            delivery.api = api

            class Req(dict):
                query: ClassVar[dict] = {'from': '2026-09-01', 'to': '2026-09-30'}
            owner = Req(miniapp_actor={'id': 4, 'roles': ['OWNER']})
            before = json.loads((await delivery.control(owner)).text)
            assert {'sale', 'delivery'} <= set(before['items'][0]['tasks'])
            async with factory() as session:
                await mark(session, await session.get(User, 4), {'OWNER'}, bid, 'NO_MONEY')
                await session.commit()
            after = json.loads((await delivery.control(owner)).text)
            item = after['items'][0]
            assert 'sale' not in item['tasks'] and 'delivery' not in item['tasks']
            assert item['noSale']['reason'] == 'NO_MONEY'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_hotel_admin_can_continue_a_stuck_sale_other_admin_cannot(tmp_path):
    async def run():
        engine, factory, bid = await _setup(tmp_path, 'takeover.sqlite')
        try:
            async with factory() as session:
                draft = await start_sale_draft(session, await session.get(User, 1), {'MANAGER'}, bid)
                await session.commit()
            from app.disk_sale_media import DiskSaleMedia
            feature = DiskSaleMedia.__new__(DiskSaleMedia)
            api = _api(engine)

            async def body(_request):
                return {}
            api.body = body
            feature.api = api

            class Req(dict):
                match_info: ClassVar[dict] = {'id': str(bid)}
            with pytest.raises(AccessError):
                await feature.takeover(Req(miniapp_actor={'id': 6, 'roles': ['ADMIN']}))
            result = json.loads((await feature.takeover(Req(miniapp_actor={'id': 5, 'roles': ['ADMIN']}))).text)
            assert result['changed'] and result['draftId'] == draft.id
            async with factory() as session:
                assert (await session.get(SaleDraft, draft.id)).created_by_id == 5
        finally:
            await engine.dispose()
    asyncio.run(run())
