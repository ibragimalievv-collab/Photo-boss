"""Photographer production actions require current approval and preserve guest viewing."""
import asyncio
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from test_workflow_services import fixture

from app.miniapp_api import MiniApp
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Client,
    ShiftCheckIn,
    ShiftCheckOut,
    Shooting,
    User,
)
from app.services.shifts import shift_now
from app.services.shooting_workflow import (
    advance_shooting,
    schedule_viewing,
    viewing_values,
)


async def setup():
    engine, factory = await fixture()
    async with factory() as session:
        session.add(Client(id=1, name='Guest', phone='+79991234567'))
        await session.flush()
        session.add(Booking(id=1, hotel_id=1, client_id=1, manager_id=1, photographer_id=2,
                            shoot_date=shift_now().date(), shoot_time=time(12), package_id=1, room='100', status='ASSIGNED'))
        session.add(Shooting(id=1, booking_id=1, status='ASSIGNED'))
        await session.commit()
    return engine, factory


def test_workflow_blocks_unapproved_and_closed_shift_and_cannot_skip_processing():
    async def run():
        engine, factory = await setup()
        try:
            async with factory() as session:
                photographer = await session.get(User, 2)
                await advance_shooting(session, photographer, 1, 'pickup')
                with pytest.raises(ValueError, match='подтвердите начало смены'):
                    await advance_shooting(session, photographer, 1, 'start')
                check = ShiftCheckIn(user_id=2, shift_date=shift_now().date(), status='STARTED', ai_status='REJECTED')
                session.add(check)
                await session.flush()
                with pytest.raises(ValueError, match='подтвердите начало смены'):
                    await advance_shooting(session, photographer, 1, 'start')
                check.ai_status = 'ACCEPTED'
                await session.flush()
                await advance_shooting(session, photographer, 1, 'start')
                await advance_shooting(session, photographer, 1, 'finish')
                with pytest.raises(ValueError, match='предыдущий'):
                    await advance_shooting(session, photographer, 1, 'ready')
                await advance_shooting(session, photographer, 1, 'processing')
                with pytest.raises(ValueError, match='время просмотра'):
                    await advance_shooting(session, photographer, 1, 'ready')
                tomorrow = (shift_now() + timedelta(days=1)).replace(second=0, microsecond=0)
                with pytest.raises(ValueError, match='комментарий'):
                    await schedule_viewing(session, photographer, 1, tomorrow.isoformat(), '')
                await schedule_viewing(session, photographer, 1, tomorrow.isoformat(), 'Гость на экскурсии')
                await advance_shooting(session, photographer, 1, 'ready')
                shooting = await session.get(Shooting, 1)
                assert shooting.processing_started_at is not None
                assert shooting.status == (await session.get(Booking, 1)).status == 'READY_FOR_SALE'
                assert shooting.viewing_comment == 'Гость на экскурсии'
                session.add(ShiftCheckOut(user_id=2, shift_date=shift_now().date(), status='FINISHED'))
                await session.flush()
                with pytest.raises(ValueError, match='завершения смены'):
                    await schedule_viewing(session, photographer, 1, tomorrow.isoformat(), 'Новый перенос')
                other = await session.get(User, 1)
                with pytest.raises(ValueError, match='не ваша'):
                    await schedule_viewing(session, other, 1, tomorrow.isoformat(), 'wrong')
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_viewing_rejects_past_date_and_requires_reason_after_shoot_day():
    now = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
    zone = ZoneInfo('Europe/Moscow')
    with pytest.raises(ValueError, match='предстоящее'):
        viewing_values('2026-10-15T10:00', '', now.date(), now=now, tz=zone)
    at, note = viewing_values('2026-10-15T12:00', '', now.date(), now=now, tz=zone)
    assert at == datetime(2026, 10, 15, 9, 0, tzinfo=UTC).replace(tzinfo=None) and note is None
    with pytest.raises(ValueError, match='комментарий'):
        viewing_values('2026-10-16T12:00', '  ', now.date(), now=now, tz=zone)
    at, note = viewing_values('2026-10-16T12:00', '  Экскурсия  ', now.date(), now=now, tz=zone)
    assert note == 'Экскурсия' and at == datetime(2026, 10, 16, 9, tzinfo=UTC).replace(tzinfo=None)


def test_api_and_workflow_list_preserve_phone_manager_and_viewing():
    async def run():
        engine, _factory = await setup()
        api = object.__new__(MiniApp)
        api.engine, api.tz = engine, ZoneInfo('Europe/Moscow')
        async def rows(conn, sql, **params):
            return list((await conn.execute(text(sql.replace(' FOR UPDATE', '')), params)).mappings())
        api.rows = rows
        try:
            async with engine.connect() as conn:
                items = await api.bookings_data(conn, {'id': 2, 'roles': ['PHOTOGRAPHER']}, shift_now().date())
                assert items[0]['clientPhone'] == '+79991234567'
                assert items[0]['managerName'] == 'Manager'
                assert items[0]['viewingAt'] is None
                assert await api.bookings_data(conn, {'id': 1, 'roles': ['PHOTOGRAPHER']}, shift_now().date()) == []
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_new_bookings_require_usable_guest_phone():
    from test_workflow_services import booking_data

    from app.services.bookings import create_booking_record
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                for phone in [None, '', '-', 'abcde', '1' * 21]:
                    with pytest.raises(ValueError, match='Проверьте данные'):
                        await create_booking_record(session, actor, booking_data() | {'client_phone': phone})
                booking = await create_booking_record(session, actor, booking_data())
                assert (await session.get(Client, booking.client_id)).phone == '+79991234567'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_miniapp_mutation_cannot_start_without_approval_or_continue_after_close():
    class Request(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.match_info = {'id': '1', 'action': 'pickup'}
    async def run():
        engine, factory = await setup()
        api = object.__new__(MiniApp)
        api.engine, api.tz = engine, ZoneInfo('Europe/Moscow')
        api.body = AsyncMock(return_value={})
        async def rows(conn, sql, **params):
            return list((await conn.execute(text(sql.replace(' FOR UPDATE', '')), params)).mappings())
        api.rows = rows
        request = Request(miniapp_actor={'id': 2, 'roles': ['PHOTOGRAPHER']})
        try:
            assert (await api.shooting_action(request)).status == 200
            request.match_info = {'id': '1', 'action': 'start'}
            with pytest.raises(AccessError, match='подтвердите начало смены'):
                await api.shooting_action(request)
            async with factory() as session:
                assert (await session.get(Shooting, 1)).status == 'PICKED_UP'
                check = ShiftCheckIn(user_id=2, shift_date=shift_now().date(), status='PENDING_REVIEW', ai_status='ACCEPTED')
                session.add(check)
                await session.commit()
            with pytest.raises(AccessError, match='подтвердите начало смены'):
                await api.shooting_action(request)
            async with factory() as session:
                check = await session.scalar(select(ShiftCheckIn))
                check.status = 'STARTED'
                await session.commit()
            assert (await api.shooting_action(request)).status == 200
            async with factory() as session:
                session.add(ShiftCheckOut(user_id=2, shift_date=shift_now().date(), status='FINISHED'))
                await session.commit()
            request.match_info = {'id': '1', 'action': 'finish'}
            with pytest.raises(AccessError, match='завершения смены'):
                await api.shooting_action(request)
            async with factory() as session:
                assert (await session.get(Shooting, 1)).status == 'SHOOTING'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_photographer_sale_start_and_final_submit_require_open_approved_shift():
    from sqlalchemy import func

    from app.models import Sale
    from app.services.sale_workflow import complete_sale, start_sale_draft
    async def run():
        engine, factory = await setup()
        try:
            async with factory() as session:
                booking = await session.get(Booking, 1)
                booking.status = 'READY_FOR_SALE'
                await session.flush()
                photographer = await session.get(User, 2)
                with pytest.raises(ValueError, match='подтвердите начало смены'):
                    await start_sale_draft(session, photographer, {'PHOTOGRAPHER'}, 1)
                session.add(ShiftCheckIn(user_id=2, shift_date=shift_now().date(), status='STARTED', ai_status='ACCEPTED'))
                await session.flush()
                draft = await start_sale_draft(session, photographer, {'PHOTOGRAPHER'}, 1)
                draft.status = 'AWAITING_SELECTED'
                session.add(ShiftCheckOut(user_id=2, shift_date=shift_now().date(), status='FINISHED'))
                await session.flush()
                with pytest.raises(ValueError, match='завершения смены'):
                    await complete_sale(session, photographer, draft.id)
                assert await session.scalar(select(func.count(Sale.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())
