"""Shared SQLite/PostgreSQL proof of feed scope, paging and exact counters."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_team import Request

from app.db import Base
from app.models import (
    Booking,
    Client,
    Hotel,
    HotelEmployee,
    Notification,
    Package,
    Sale,
    SaleDiscountApproval,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
    WorkBreak,
)
from app.notification_feed import visibility
from app.push_notifications import KINDS, AppPush, eligible, queue_notice
from app.services.booking_reminders import scheduled_at

NOW = datetime(2026, 10, 29, 9, 30, tzinfo=UTC)


def request(uid, body=None, query=None):
    r = Request({'id': uid, 'roles': ['OWNER']}, body)
    r.query = query or {}
    return r


async def scope_fixture(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        roles = {1: {'MANAGER'}, 2: {'PHOTOGRAPHER'}, 3: {'OWNER'}, 4: {'ADMIN'},
                 5: {'MANAGER'}, 6: {'PHOTOGRAPHER'}}
        session.add_all([User(id=i, tg_id=i, name=f'Synthetic {i}') for i in roles])
        await session.flush()
        session.add_all(UserRole(user_id=uid, role=role) for uid, rs in roles.items() for role in rs)
        session.add_all([Hotel(id=1, name='Allowed'), Hotel(id=2, name='Other'),
            Client(id=1, name='Synthetic guest'), Package(id=1, name='Standard', price_per_photo=400)])
        await session.flush()
        session.add_all(HotelEmployee(hotel_id=hid, user_id=uid) for uid, hid in [(1, 1), (2, 1), (4, 1), (5, 2), (6, 2)])
        bookings = [Booking(id=i, hotel_id=i, client_id=1, room='1', deposit=0,
            shoot_date=NOW.date(), shoot_time=NOW.time().replace(tzinfo=None), package_id=1,
            manager_id=1 if i == 1 else 5, photographer_id=2 if i == 1 else 6, status='ASSIGNED') for i in (1, 2)]
        session.add_all(bookings)
        await session.flush()
        session.add_all([
            Shift(id=1, user_id=1, hotel_id=1, start_at=NOW.replace(tzinfo=None)-timedelta(hours=2), end_at=NOW.replace(tzinfo=None)+timedelta(hours=2)),
            Shift(id=2, user_id=2, hotel_id=1, start_at=NOW.replace(tzinfo=None)-timedelta(hours=2), end_at=NOW.replace(tzinfo=None)+timedelta(hours=2)),
            ShiftCheckIn(id=1, user_id=1, shift_date=NOW.date(), status='STARTED'),
            ShiftCheckIn(id=2, user_id=2, shift_date=NOW.date(), status='PENDING_REVIEW'),
            ShiftCheckOut(id=1, user_id=2, shift_date=NOW.date(), status='PENDING_REVIEW'),
            WorkBreak(id=1, user_id=2, hotel_id=1, shift_date=NOW.date(), started_at=NOW.replace(tzinfo=None)),
            Sale(id=1, booking_id=1, created_by_id=1, credited_user_id=2, sold_photos=1, amount=400, percent=0, commission=0),
            SaleDiscountApproval(id=1, booking_id=1, requested_by_id=2, total_photos=10, unit_price=400),
            Setting(key='notify:owner:3', value='{"daily":true,"critical":true}'),
            Setting(key='attendance:fine:2026-10-29:2:late', value=json.dumps({'userId': 2, 'date': '2026-10-29', 'kind': 'late'})),
        ])
        await session.flush()
        cases = [
            ('chat', {}, None), ('call', {}, None), ('academy', {}, None), ('push_test', {}, None),
            ('work_event', {}, None), ('service_status', {}, None),
            ('operational_alert', {'bookingId': 1}, None), ('operational_alert', {'bookingId': 2}, None),
            ('operational_alert', {'bookingId': '1'}, None),
            ('booking_assignment', {'bookingId': 1}, None),
            ('booking_assignment', {'bookingId': 2}, None),
            ('booking_reminder', {'bookingId': 1, 'scheduledAt': scheduled_at(bookings[0]).isoformat()}, None),
            ('booking_reminder', {'bookingId': 1, 'scheduledAt': 'wrong'}, None),
            ('sale_ready', {'bookingId': 1}, None), ('sale_ready', {'bookingId': 2}, None),
            ('sale_ready', {}, None), ('reminder', {}, None),
            ('reminder', {'bookingId': 1}, None), ('reminder', {'bookingId': 2}, None),
            ('lunch_start', {'workBreakId': 1}, None), ('lunch_end', {'workBreakId': 1}, None),
            ('lunch_corrected', {'workBreakId': 1}, None), ('lunch_attention', {'workBreakId': 1}, None),
            ('attendance', {'purpose': 'start', 'attendanceId': 2, 'userId': 2, 'date': '2026-10-29'}, None),
            ('attendance', {'purpose': 'end', 'attendanceId': 1, 'userId': 2, 'date': '2026-10-29'}, None),
            ('attendance', {'purpose': 'start', 'attendanceId': 2, 'userId': 1, 'date': '2026-10-29'}, None),
            ('attendance_fine', {'userId': 2, 'date': '2026-10-29', 'fineKind': 'late', 'fineKey': 'attendance:fine:2026-10-29:2:late'}, None),
            ('sale_discount', {'discountId': 1, 'bookingId': 1}, None),
            ('sale_discount', {'discountId': 1, 'bookingId': 2}, None),
            ('sale_created', {'bookingId': 1}, 'sale-created:1'),
            ('sale_created', {'bookingId': 1}, 'legacy-sale:1'),
            ('sale_created', {'bookingId': 1}, 'sale-created:1bad'),
            ('manager_advice', {'hotelIds': [1]}, None), ('manager_advice', {'hotelIds': [1, 2]}, None),
            ('manager_advice', {'hotelIds': ['1']}, None),
            ('manager_idle', {'hotelIds': [1], 'managerId': 1, 'shiftId': 1}, None),
            ('manager_idle', {'hotelIds': [1], 'managerId': 5, 'shiftId': 1}, None),
            ('daily_summary', {}, None), ('control', {}, None),
        ]
        assert {kind for kind, _payload, _key in cases} == KINDS, 'Add scope parity coverage for the new notification kind'
        for uid in roles:
            for index, (kind, payload, key) in enumerate(cases):
                session.add(Notification(user_id=uid, kind=kind, event_key=key or f'synthetic:{index}',
                    payload=json.dumps(payload), text=f'Case {index}', priority='critical' if kind == 'control' else 'info'))
            session.add(Notification(user_id=uid, kind='sale_ready', text='Съёмка №1 готова к продаже.', payload=None))
            session.add(Notification(user_id=uid, kind='operational_alert', text='Malformed private payload', payload='bad json'))
        await session.commit()
    return factory, roles


async def check_scope(engine):
    factory, roles = await scope_fixture(engine)
    async with factory() as session:
        for uid, rs in roles.items():
            actor = await session.get(User, uid)
            notices = list(await session.scalars(select(Notification).where(Notification.user_id == uid)))
            expected = {n.id for n in notices if await eligible(session, n,
                SimpleNamespace(active=True, user_id=uid), include_read=True)}
            actual = set(await session.scalars(select(Notification.id).where(await visibility(session, actor, rs))))
            assert actual == expected, {'user': uid, 'extra': sorted(actual-expected), 'missing': sorted(expected-actual)}
        # Changes to assignment, scopes, planned hotels and preference apply on
        # the next poll; no persisted cache can leak historical authorization.
        (await session.get(Booking, 1)).photographer_id = 6
        session.add(Shift(id=3, user_id=2, hotel_id=2, start_at=NOW.replace(tzinfo=None)-timedelta(hours=1),
            end_at=NOW.replace(tzinfo=None)+timedelta(hours=1)))
        session.add(Setting(key='booking-control:settings', value='{"enabled":false}'))
        await session.commit()
        for uid, rs in roles.items():
            actor = await session.get(User, uid)
            notices = list(await session.scalars(select(Notification).where(Notification.user_id == uid)))
            expected = {n.id for n in notices if await eligible(session, n,
                SimpleNamespace(active=True, user_id=uid), include_read=True)}
            actual = set(await session.scalars(select(Notification.id).where(await visibility(session, actor, rs))))
            assert actual == expected, {'revokedUser': uid, 'extra': sorted(actual-expected), 'missing': sorted(expected-actual)}


async def check_large_feed(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([User(id=1, tg_id=1, name='Synthetic manager'), User(id=2, tg_id=2, name='Other manager')])
        await session.flush()
        session.add_all([UserRole(user_id=1, role='MANAGER'), UserRole(user_id=2, role='MANAGER')])
        session.add_all(Notification(user_id=1, kind='chat', event_key=f'event:{i}', text='Same text, distinct event',
            acknowledged_at=None if i < 250 else NOW.replace(tzinfo=None)) for i in range(2500))
        session.add_all(Notification(user_id=1, kind='service_status', event_key=f'noise:{i}', text='Owner-only service status') for i in range(100))
        session.add_all(Notification(user_id=1, kind='chat', event_key=f'resolved:{i}', text='Resolved', resolved_at=NOW.replace(tzinfo=None)) for i in range(50))
        session.add(Notification(user_id=2, kind='chat', event_key='other', text='Other account'))
        await session.commit()
    api = SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda r: r.body))
    service = AppPush(api)
    statements = []
    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine.sync_engine, 'before_cursor_execute', record)
    try:
        first = json.loads((await service.status(request(1))).text)
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', record)
    assert len(first['items']) == 12 and first['unreadCount'] == 250 and first['hasMore']
    assert first['latestId'] == max(n['id'] for n in first['items'])
    assert len(statements) <= 13, f'Page introduced N+1 queries: {len(statements)}'
    notice_queries = [q.upper() for q in statements if q.lstrip().upper().startswith('SELECT') and 'FROM NOTIFICATIONS' in q.upper()]
    assert len(notice_queries) == 2 and any('COUNT(' in q for q in notice_queries)
    assert any('LIMIT' in q for q in notice_queries), 'History was materialized without a page limit'
    older = json.loads((await service.status(request(1, query={'before': first['olderCursor'], 'limit': '12'}))).text)
    assert len(older['items']) == 12 and older['unreadCount'] == 250
    assert not {n['id'] for n in first['items']} & {n['id'] for n in older['items']}
    assert all(n['id'] < int(first['olderCursor']) for n in older['items'])
    async with factory() as session:
        later = await queue_notice(session, 1, kind='chat', event_key='later', message='New arrival')
        foreign = await session.scalar(select(Notification.id).where(Notification.user_id == 2))
        await session.commit()
    # Marking an already read page item is harmless; read-all is inclusive at
    # the captured head and leaves a later event and another account untouched.
    read = json.loads((await service.read(request(1, {'id': first['items'][0]['id']}))).text)
    assert read['unreadCount'] == 251
    result = json.loads((await service.read_all(request(1, {'throughId': first['latestId']}))).text)
    assert result['unreadCount'] == 1
    assert json.loads((await service.read_all(request(1, {'throughId': first['latestId']}))).text)['unreadCount'] == 1
    async with factory() as session:
        assert (await session.get(Notification, later)).acknowledged_at is None
        assert (await session.get(Notification, foreign)).acknowledged_at is None
        assert await session.scalar(select(func.count(Notification.id)).where(Notification.kind == 'service_status', Notification.acknowledged_at.is_(None))) == 100
    reloaded = json.loads((await AppPush(api).status(request(1))).text)
    assert reloaded['unreadCount'] == 1 and reloaded['items'][0]['id'] == later
    return {'history': 2651, 'page': len(first['items']), 'queries': len(statements), 'unread': 250}
