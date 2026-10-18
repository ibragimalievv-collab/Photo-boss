"""An operational reset must not manufacture absences or revive archived alerts."""
import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import booking_control
from app.db import Base
from app.insights import daily_control
from app.models import (
    AuditLog,
    Hotel,
    Notification,
    Sale,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
)
from app.services import discipline
from app.services.insights import anomalies


def at(day, hour=0):
    return datetime(2026, 10, day, hour, tzinfo=UTC).replace(tzinfo=None)


async def setup():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([
            User(id=1, tg_id=1, name='Владелец'),
            User(id=2, tg_id=2, name='Менеджер'),
            Hotel(id=1, name='Бетон'),
            UserRole(user_id=1, role='OWNER'),
            UserRole(user_id=2, role='MANAGER'),
        ])
        await session.commit()

    async def rows(conn, sql, **params):
        return list((await conn.execute(text(sql), params)).mappings())

    return engine, factory, SimpleNamespace(rows=rows, tz=ZoneInfo('Europe/Moscow'),
                                          today=lambda: date(2026, 10, 19))


def test_reset_ignores_old_attendance_and_completed_plans_but_keeps_current_schedule(monkeypatch):
    monkeypatch.setattr(discipline, 'utc_now', lambda: at(19, 18))

    async def run():
        engine, factory, api = await setup()
        try:
            async with factory() as session:
                session.add(Setting(key='operations:reset_at', value='2026-10-18T11:00:00+00:00'))
                session.add_all([
                    Shift(id=1, user_id=2, hotel_id=1, start_at=at(17, 6), end_at=at(17, 15)),
                    Shift(id=2, user_id=2, hotel_id=1, start_at=at(18, 6), end_at=at(18, 15)),
                    Shift(id=3, user_id=2, hotel_id=1, start_at=at(19, 6), end_at=at(19, 15)),
                    ShiftCheckIn(user_id=2, shift_date=date(2026, 10, 17), status='STARTED',
                                 late=True, started_at=at(17, 7), initiated_at=at(17, 7)),
                    ShiftCheckIn(user_id=2, shift_date=date(2026, 10, 18), status='STARTED',
                                 late=False, started_at=at(18, 12), initiated_at=at(18, 12)),
                    ShiftCheckOut(user_id=2, shift_date=date(2026, 10, 18), status='FINISHED',
                                  ended_at=at(18, 15), initiated_at=at(18, 15)),
                ])
                await session.commit()
            async with engine.connect() as conn:
                user = (await discipline.period(api, conn, date(2026, 10, 17), date(2026, 10, 19)))[2]
                assert user['late'] == user['unclosed'] == 0
                assert user['confirmed'] == user['closed'] == 1
                assert user['missedShifts'] == [{'shiftId': 3, 'date': '2026-10-19'}]
                assert [day['date'] for day in user['trend']] == ['2026-10-18', '2026-10-19']
                assert await conn.scalar(select(func.count()).select_from(Shift)) == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_archived_sale_audit_cannot_recreate_deleted_sale_alert():
    async def run():
        engine, factory, api = await setup()
        try:
            async with factory() as session:
                session.add(Sale(id=10, created_by_id=2, credited_user_id=2, sold_photos=1,
                                 amount=400, percent=0, commission=0, created_at=at(19, 9)))
                for sid in (9, 10):
                    session.add(AuditLog(action='row.update', entity='sales', entity_id=sid,
                                         details=json.dumps({'before': {'amount': 400},
                                                             'after': {'amount': 800}})))
                await session.commit()
            async with engine.connect() as conn:
                changed = [f for f in await anomalies(api, conn) if f['key'].startswith('sale-audit:')]
                assert [f['entityId'] for f in changed] == [10]
                assert await conn.scalar(select(func.count()).select_from(AuditLog)) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reset_does_not_regenerate_daily_digest_for_archived_day():
    async def run():
        engine, factory, _api = await setup()
        try:
            async with factory() as session:
                session.add(Setting(key='operations:reset_at', value='2026-10-18T11:00:00Z'))
                await session.commit()
                assert await daily_control(session, at(18, 12).replace(tzinfo=UTC), 'Europe/Moscow') == 0
                await session.commit()
                assert await session.scalar(select(func.count()).select_from(Notification).where(
                    Notification.kind == 'daily_summary')) == 0
                assert await daily_control(session, at(19, 7).replace(tzinfo=UTC), 'Europe/Moscow') == 1
                await session.commit()
                digest = await session.scalar(select(Notification).where(Notification.kind == 'daily_summary'))
                assert digest.event_key == 'daily:2026-10-18'
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reset_ignores_old_checkin_for_idle_reminders_until_employee_restarts(monkeypatch):
    monkeypatch.setattr(booking_control, 'config', SimpleNamespace(
        training_timezone='Europe/Moscow', openai_api_key=None))

    async def run():
        engine, factory, _api = await setup()
        bot = SimpleNamespace(send_message=AsyncMock())
        now = at(18, 13).replace(tzinfo=UTC)
        try:
            async with factory() as session:
                session.add_all([
                    Setting(key='operations:reset_at', value='2026-10-18T11:00:00+00:00'),
                    Shift(user_id=2, hotel_id=1, start_at=at(18, 6), end_at=at(18, 16)),
                    ShiftCheckIn(user_id=2, shift_date=date(2026, 10, 18), status='STARTED',
                                 started_at=at(18, 6), initiated_at=at(18, 6)),
                ])
                await session.commit()
                assert (await booking_control.activity(session, now=now))['managers'] == []
                assert await booking_control.activity_tick(session, bot, now) == 0
                bot.send_message.assert_not_awaited()
                checkin = await session.scalar(select(ShiftCheckIn))
                checkin.started_at = checkin.initiated_at = at(18, 12)
                await session.commit()
                data = await booking_control.activity(session, now=now)
                assert [m['user'].id for m in data['managers']] == [2]
                assert data['managers'][0]['idleMinutes'] == 60
        finally:
            await engine.dispose()

    asyncio.run(run())
