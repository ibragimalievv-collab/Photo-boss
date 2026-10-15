"""Cross-module photographer regressions against an isolated database."""
import asyncio
import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from test_insights import fixture

from app.models import (
    Hotel,
    PayrollEntry,
    Photo,
    PhotoStorage,
    Sale,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    UserRole,
)
from app.services.insights import anomalies


def timestamp(*args):
    return datetime(*args, tzinfo=UTC).replace(tzinfo=None)


def test_one_hotel_checkout_never_hides_another_open_shift():
    async def run():
        engine, factory, api = await fixture()
        try:
            async with factory() as session:
                session.add(Hotel(id=2, name='Second hotel'))
                session.add_all([
                    Shift(id=1, user_id=2, hotel_id=1, start_at=timestamp(2026, 9, 20, 6), end_at=timestamp(2026, 9, 20, 10)),
                    Shift(id=2, user_id=2, hotel_id=2, start_at=timestamp(2026, 9, 20, 11), end_at=timestamp(2026, 9, 20, 15)),
                ])
                await session.flush()
                session.add_all([
                    ShiftCheckIn(id=10, user_id=2, shift_date=date(2026, 9, 20), scheduled_shift_id=1, status='STARTED', started_at=timestamp(2026, 9, 20, 6)),
                    ShiftCheckIn(id=11, user_id=2, shift_date=date(2026, 9, 20), scheduled_shift_id=2, status='STARTED', started_at=timestamp(2026, 9, 20, 11)),
                    ShiftCheckOut(id=10, user_id=2, shift_date=date(2026, 9, 20), scheduled_shift_id=1, status='FINISHED', ended_at=timestamp(2026, 9, 20, 10)),
                    # A legacy unscoped checkout must not close the second hotel's shift.
                    ShiftCheckOut(id=12, user_id=2, shift_date=date(2026, 9, 20), status='FINISHED', ended_at=timestamp(2026, 9, 20, 15)),
                ])
                await session.commit()
            async with engine.connect() as conn:
                items = await anomalies(api, conn)
            open_items = [item for item in items if item['title'] == 'Смена не закрыта']
            assert [(item['entityId'], item['evidence']['shiftId']) for item in open_items] == [(11, 2)]
            async with factory() as session:
                # Pending review of this exact shift suppresses only this alert.
                session.add(ShiftCheckOut(user_id=2, shift_date=date(2026, 9, 20), scheduled_shift_id=2, status='PENDING_REVIEW', offline_claimed_at=timestamp(2026, 9, 20, 15), location_received_at=timestamp(2026, 9, 20, 16)))
                await session.commit()
                payroll_before = await session.scalar(select(func.count(PayrollEntry.id)))
            async with engine.connect() as conn:
                items = await anomalies(api, conn)
            assert not any(item['title'] == 'Смена не закрыта' for item in items)
            pending = next(item for item in items if item['title'] == 'Офлайн-отметка ожидает проверки')
            assert pending['evidence']['shiftId'] == 2
            async with factory() as session:
                assert await session.scalar(select(func.count(PayrollEntry.id))) == payroll_before
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_daily_checklist_alert_is_single_with_multiple_hotel_closures():
    async def run():
        engine, factory, api = await fixture()
        try:
            async with factory() as session:
                session.add_all([
                    UserRole(user_id=2, role='PHOTOGRAPHER'),
                    Shift(id=1, user_id=2, hotel_id=1, start_at=timestamp(2026, 9, 20, 6), end_at=timestamp(2026, 9, 20, 10)),
                    Shift(id=2, user_id=2, hotel_id=1, start_at=timestamp(2026, 9, 20, 11), end_at=timestamp(2026, 9, 20, 15)),
                    Setting(key='checklist:equipment', value=json.dumps({'active': True, 'required': True, 'roles': ['PHOTOGRAPHER'], 'title': 'Проверить оборудование', 'effectiveFrom': '2026-09-01'})),
                ])
                await session.flush()
                session.add_all([
                    ShiftCheckOut(user_id=2, shift_date=date(2026, 9, 20), scheduled_shift_id=1, status='FINISHED'),
                    ShiftCheckOut(user_id=2, shift_date=date(2026, 9, 20), scheduled_shift_id=2, status='FINISHED'),
                ])
                await session.commit()
            async with engine.connect() as conn:
                items = await anomalies(api, conn)
            checklist_items = [item for item in items if item['key'].startswith('required:')]
            assert len(checklist_items) == 1
            assert checklist_items[0]['evidence']['itemKey'] == 'checklist:equipment'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_bot_and_api_share_exact_hotel_and_ai_approval_boundary(monkeypatch):
    """The API SQL and bot ORM must make the same decision for every boundary."""
    from app.services import shifts

    monkeypatch.setattr(shifts, 'shift_now', lambda value=None: datetime(2026, 9, 20, 12, tzinfo=UTC))

    async def run():
        engine, factory, api = await fixture()
        try:
            async with factory() as session:
                session.add(Hotel(id=2, name='Second hotel'))
                session.add_all([
                    Shift(id=1, user_id=2, hotel_id=1, start_at=timestamp(2026, 9, 20, 6), end_at=timestamp(2026, 9, 20, 10)),
                    Shift(id=2, user_id=2, hotel_id=2, start_at=timestamp(2026, 9, 20, 11), end_at=timestamp(2026, 9, 20, 15)),
                    # A daily legacy check-in cannot authorize scheduled hotel work.
                    ShiftCheckIn(user_id=2, shift_date=date(2026, 9, 20), status='STARTED', ai_status='ACCEPTED', started_at=timestamp(2026, 9, 20, 6)),
                ])
                await session.commit()

            async def boundary(hotel_id, permitted):
                async with factory() as session:
                    if permitted:
                        orm = await shifts.require_active_shift(session, 2, hotel_id)
                        sql = await shifts.require_active_shift_conn(api, session, 2, hotel_id)
                        assert orm.id == sql['id']
                    else:
                        with pytest.raises(ValueError, match='Сначала подтвердите'):
                            await shifts.require_active_shift(session, 2, hotel_id)
                        with pytest.raises(ValueError, match='Сначала подтвердите'):
                            await shifts.require_active_shift_conn(api, session, 2, hotel_id)

            await boundary(1, False)
            await boundary(2, False)
            async with factory() as session:
                session.add_all([
                    ShiftCheckIn(id=10, user_id=2, shift_date=date(2026, 9, 20), scheduled_shift_id=1, status='STARTED', ai_status='ACCEPTED', started_at=timestamp(2026, 9, 20, 6)),
                    ShiftCheckIn(id=11, user_id=2, shift_date=date(2026, 9, 20), scheduled_shift_id=2, status='STARTED', ai_status='UNAVAILABLE', started_at=timestamp(2026, 9, 20, 11)),
                ])
                await session.commit()
            await boundary(1, True)
            await boundary(2, False)
            async with factory() as session:
                (await session.get(ShiftCheckIn, 11)).ai_status = 'MANUAL_APPROVED'
                session.add(ShiftCheckOut(user_id=2, shift_date=date(2026, 9, 20), scheduled_shift_id=1, status='FINISHED', ended_at=timestamp(2026, 9, 20, 10)))
                await session.commit()
            await boundary(1, False)
            await boundary(2, True)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_control_uses_same_unique_frames_as_final_commission():
    async def run():
        engine, factory, api = await fixture()
        try:
            async with factory() as session:
                sale = await session.get(Sale, 1)
                sale.percent, sale.commission = 10, 80
                for index in range(150):
                    photo = Photo(shooting_id=1, file_id=f'file-{index}')
                    session.add(photo)
                    await session.flush()
                    # 150 database rows contain 149 distinct source files.
                    session.add(PhotoStorage(photo_id=photo.id, shooting_id=1,
                        telegram_file_id=photo.file_id, telegram_unique_id=f'unique-{index}',
                        source_kind='DOCUMENT', sha256=f'{min(index, 148):064d}', status='STORED'))
                await session.commit()
            async with engine.connect() as conn:
                items = await anomalies(api, conn)
            assert not any(item['key'] == 'sale:1:commission' for item in items)
        finally:
            await engine.dispose()
    asyncio.run(run())
