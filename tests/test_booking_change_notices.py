"""Booking changes and their app notices must survive the same commit."""
import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_team import Request
from test_workflow_services import booking_data, fixture

from app.booking_management import BookingManagement, snapshot, version
from app.handlers import manager
from app.miniapp_security import AccessError
from app.models import Booking, Client, Notification, Shooting, User
from app.services.bookings import create_booking_record


async def assigned_booking(factory):
    async with factory() as session:
        booking = await create_booking_record(session, await session.get(User, 1), booking_data())
        booking.photographer_id, booking.status = 2, 'ASSIGNED'
        await session.commit()
        return booking.id


async def current_revision(factory, bid):
    async with factory() as session:
        booking = await session.get(Booking, bid)
        return version(snapshot(booking, await session.get(Client, booking.client_id),
            await session.scalar(select(Shooting).where(Shooting.booking_id == bid))))


@pytest.mark.parametrize('mode', ['edit', 'cancel'])
def test_miniapp_change_commits_notice_and_replay_cannot_duplicate(mode):
    async def run():
        engine, factory = await fixture()
        try:
            bid = await assigned_booking(factory)
            api = SimpleNamespace(engine=engine, today=lambda: date(2026, 9, 21),
                                  body=AsyncMock(side_effect=lambda request: request.body))
            body = {'mode': mode, 'revision': await current_revision(factory, bid), 'reason': 'По просьбе гостя'}
            if mode == 'edit':
                body.update(client='Guest', phone='', room='100', guestCount=2,
                            date='2026-09-23', time='14:30')
            request = Request({'id': 1, 'roles': ['MANAGER']}, body, bid)
            await BookingManagement(api).manage(request)
            with pytest.raises(AccessError, match='уже изменена'):
                await BookingManagement(api).manage(request)
            async with factory() as session:
                notices = list(await session.scalars(select(Notification)))
                assert len(notices) == 1
                notice = notices[0]
                assert notice.user_id == 2 and not notice.sent
                assert notice.event_key.startswith(('rescheduled:' if mode == 'edit' else 'cancelled:') + str(bid) + ':')
                assert json.loads(notice.payload)['bookingId'] == bid
                if mode == 'edit':
                    assert '23.09.2026' in notice.text and '14:30' in notice.text
                else:
                    assert 'По просьбе гостя' in json.loads(notice.payload)['description']
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['reschedule', 'cancel'])
def test_bot_app_channel_change_notice_survives_session_close(monkeypatch, mode):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    async def run():
        engine, factory = await fixture()
        monkeypatch.setattr(manager, 'Session', factory)
        try:
            bid = await assigned_booking(factory)
            state = SimpleNamespace(clear=AsyncMock(), get_data=AsyncMock(return_value={
                'reschedule_booking_id': bid, 'reschedule_date': '2026-09-23',
                'cancel_booking_id': bid, 'cancel_reason': 'По просьбе гостя'}))
            event = SimpleNamespace(text='14:30', from_user=SimpleNamespace(id=1),
                data=f'booking:cancel_confirm:{bid}', answer=AsyncMock(),
                message=SimpleNamespace(answer=AsyncMock()), bot=SimpleNamespace(send_message=AsyncMock()))
            if mode == 'reschedule':
                await manager.finish_reschedule(event, state, {'MANAGER'})
            else:
                await manager.cancel_confirm(event, state, {'MANAGER'})
            async with factory() as session:
                notices = list(await session.scalars(select(Notification)))
                assert len(notices) == 1
                assert notices[0].user_id == 2 and not notices[0].sent
                assert json.loads(notices[0].payload)['bookingId'] == bid
            event.bot.send_message.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['reschedule', 'cancel'])
@pytest.mark.parametrize('started', [True, False])
def test_stale_bot_change_cannot_erase_shooting_work_or_sale(monkeypatch, mode, started):
    from app.models import Sale, utc_now
    async def run():
        engine, factory = await fixture()
        monkeypatch.setattr(manager, 'Session', factory)
        try:
            bid = await assigned_booking(factory)
            async with factory() as session:
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                if started:
                    shooting.status, shooting.started_at = 'SHOOTING', utc_now()
                else:
                    # Legacy/mismatched booking status must not bypass the sale guard.
                    session.add(Sale(booking_id=bid, created_by_id=1, credited_user_id=1,
                        commission_role='MANAGER', sold_photos=1, amount=400, percent=15, commission=60))
                await session.commit()
            state = SimpleNamespace(clear=AsyncMock(), get_data=AsyncMock(return_value={
                'reschedule_booking_id': bid, 'reschedule_date': '2026-09-23',
                'cancel_booking_id': bid, 'cancel_reason': 'По просьбе гостя'}))
            event = SimpleNamespace(text='14:30', from_user=SimpleNamespace(id=1),
                data=f'booking:cancel_confirm:{bid}', answer=AsyncMock(),
                message=SimpleNamespace(answer=AsyncMock()), bot=SimpleNamespace(send_message=AsyncMock()))
            if mode == 'reschedule':
                await manager.finish_reschedule(event, state, {'MANAGER'})
            else:
                await manager.cancel_confirm(event, state, {'MANAGER'})
            async with factory() as session:
                booking = await session.get(Booking, bid)
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                assert booking.status == 'ASSIGNED' and booking.shoot_date == date(2026, 9, 22)
                if started:
                    assert shooting.status == 'SHOOTING' and shooting.started_at
                assert not list(await session.scalars(select(Notification)))
            event.bot.send_message.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_bot_ready_for_sale_notice_commits_with_status_and_is_not_duplicated(monkeypatch):
    from app.handlers import photographer
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL', 'app')
    async def run():
        engine, factory = await fixture()
        monkeypatch.setattr(photographer, 'Session', factory)
        try:
            bid = await assigned_booking(factory)
            async with factory() as session:
                shoot = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                shoot.status = 'SHOT'
                sid = shoot.id
                await session.commit()
            event = SimpleNamespace(from_user=SimpleNamespace(id=2), data=f'photo:ready:{sid}',
                answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock()),
                bot=SimpleNamespace(send_message=AsyncMock()))
            state = SimpleNamespace(clear=AsyncMock())
            await photographer.action(event, state)
            await photographer.action(event, state)
            async with factory() as session:
                assert (await session.get(Booking, bid)).status == 'READY_FOR_SALE'
                notices = list(await session.scalars(select(Notification).where(Notification.kind == 'sale_ready')))
                assert {n.user_id for n in notices} == {1, 2} and len(notices) == 2
            event.bot.send_message.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_repeated_move_back_to_same_date_gets_a_new_notice():
    async def run():
        engine, factory = await fixture()
        try:
            bid = await assigned_booking(factory)
            api = SimpleNamespace(engine=engine, today=lambda: date(2026, 9, 21),
                                  body=AsyncMock(side_effect=lambda request: request.body))
            for day in ('2026-09-23', '2026-09-22', '2026-09-23'):
                body = {'mode': 'edit', 'revision': await current_revision(factory, bid),
                    'reason': 'По просьбе гостя', 'client': 'Guest', 'phone': '', 'room': '100',
                    'guestCount': 2, 'date': day, 'time': '14:30'}
                await BookingManagement(api).manage(Request({'id': 1, 'roles': ['MANAGER']}, body, bid))
            async with factory() as session:
                notices = list(await session.scalars(select(Notification)))
                assert len(notices) == len({notice.event_key for notice in notices}) == 3
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_notice_failure_rolls_back_booking_change(monkeypatch):
    from app.services import booking_notices
    monkeypatch.setattr(booking_notices, 'queue_photographer_change',
                        AsyncMock(side_effect=RuntimeError('synthetic queue failure')))
    async def run():
        engine, factory = await fixture()
        try:
            bid = await assigned_booking(factory)
            revision = await current_revision(factory, bid)
            api = SimpleNamespace(engine=engine, today=lambda: date(2026, 9, 21),
                                  body=AsyncMock(side_effect=lambda request: request.body))
            body = {'mode': 'cancel', 'revision': revision, 'reason': 'По просьбе гостя'}
            with pytest.raises(RuntimeError, match='synthetic queue failure'):
                await BookingManagement(api).manage(Request({'id': 1, 'roles': ['MANAGER']}, body, bid))
            assert await current_revision(factory, bid) == revision
        finally:
            await engine.dispose()
    asyncio.run(run())
