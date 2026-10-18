"""Cross-hotel reads and mutations use object grants, never just ADMIN role."""
import asyncio
import json
import unittest
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_miniapp_release as fixtures
from sqlalchemy import select, text
from test_offline_attendance import NOW, RAW, prepare

from app.miniapp_security import AccessError
from app.models import (
    Hotel,
    HotelEmployee,
    PayrollEntry,
    Shift,
    ShiftCheckIn,
    User,
    UserRole,
)


class AdminScheduleScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.MiniAppTests()
        await self.fixture.asyncSetUp()
        self.api, self.day = self.fixture.service, self.fixture.service.today()
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text('INSERT INTO hotel_employees VALUES (2,1)'))

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def slot(self, *, hotel=1, user=3, offset=0):
        return {'userId': user, 'hotelId': hotel, 'date': str(self.day + timedelta(days=offset)),
                'start': '09:00', 'end': '18:00'}

    async def create(self, payload, *, uid=1001):
        return await self.fixture.call('/schedule', uid=uid, method='POST', body=payload)

    async def test_admin_reads_only_assigned_hotels_but_keeps_full_staff_roster(self):
        assert (await self.create(self.slot()))[0] == 201
        assert (await self.create(self.slot(hotel=2, user=5)))[0] == 201
        _, admin, _ = await self.fixture.call('/schedule', uid=1002)
        _, owner, _ = await self.fixture.call('/schedule')
        assert {r['hotelId'] for r in admin['items']} == {1}
        assert {r['id'] for r in admin['hotels']} == {1}
        assert {r['id'] for r in admin['employees']} == {3, 4, 5}
        assert {r['hotelId'] for r in owner['items']} == {1, 2}
        assert {r['id'] for r in owner['hotels']} == {1, 2}
        _, dashboard, _ = await self.fixture.call('/dashboard', uid=1002)
        assert {r['hotelId'] for r in dashboard['team']} == {1}

    async def test_admin_may_plan_unassigned_staff_only_inside_own_hotels(self):
        # User 5 has no hotel membership. A new employee must stay selectable.
        assert (await self.create(self.slot(user=5), uid=1002))[0] == 201
        assert (await self.create(self.slot(hotel=2, offset=1), uid=1002))[0] == 403
        status, foreign, _ = await self.create(self.slot(hotel=2, offset=1))
        assert status == 201
        path = '/schedule/' + str(foreign['id'])
        assert (await self.fixture.call(path, uid=1002, method='DELETE'))[0] == 403
        assert (await self.fixture.call(path, method='DELETE'))[0] == 200
        # An already-cancelled foreign object does not bypass the grant check.
        assert (await self.fixture.call(path, uid=1002, method='DELETE'))[0] == 403

    async def test_mixed_team_batch_rolls_back_and_current_role_is_rechecked(self):
        request = fixtures.Request('/api/miniapp/schedule/team', 1002, 'POST',
            {'shifts': [self.slot(), self.slot(hotel=2, user=5)]}, None)
        response = await self.api.middleware(request, self.api.create_team_schedule)
        assert response.status == 403
        with self.fixture.engine.inner.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM shifts')).scalar_one() == 0
            assert conn.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='miniapp_shift_created'")).scalar_one() == 0
        request = fixtures.Request('/api/miniapp/schedule', 1002, 'POST', self.slot(), None)
        request['miniapp_actor'] = {'id': 2, 'roles': ['ADMIN']}
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text("DELETE FROM user_roles WHERE user_id=2"))
        with pytest.raises(AccessError):
            await self.api.create_shift(request)


class AttendanceRequest(dict):
    def __init__(self, actor, **match_info):
        super().__init__(miniapp_actor=actor)
        self.match_info = match_info


def test_attendance_admin_scope_uses_day_plan_not_membership_and_preserves_owner(monkeypatch):
    async def run():
        engine, factory, work, attendance = await prepare(monkeypatch)
        admin, owner = {'id': 4, 'roles': ['ADMIN']}, {'id': 3, 'roles': ['OWNER']}
        cases = {
            5: (NOW.date(), [(1, 'PLANNED')]),
            6: (NOW.date(), [(2, 'PLANNED')]),
            7: (NOW.date(), [(1, 'PLANNED'), (2, 'PLANNED')]),
            8: (NOW.date(), []),
            9: (NOW.date(), [(1, 'CANCELLED')]),
            10: (NOW.date()-timedelta(days=2), [(1, 'PLANNED')]),
            11: (NOW.date(), [(1, 'PLANNED'), (2, 'CANCELLED')]),
        }
        try:
            async with factory() as session:
                session.add_all([Hotel(id=2, name='Other hotel'), User(id=4, tg_id=4, name='Admin'),
                    UserRole(user_id=4, role='ADMIN'), HotelEmployee(user_id=4, hotel_id=1)])
                for uid, (day, plans) in cases.items():
                    session.add_all([User(id=uid, tg_id=uid, name=f'Photographer {uid}'),
                        UserRole(user_id=uid, role='PHOTOGRAPHER'),
                        # Membership cannot grant access to a different/unlinked day's attendance.
                        HotelEmployee(user_id=uid, hotel_id=1),
                        ShiftCheckIn(id=uid, user_id=uid, shift_date=day, status='PENDING_REVIEW',
                            initiated_at=NOW.replace(tzinfo=None), latitude=45, longitude=39,
                            location_received_at=NOW.replace(tzinfo=None),
                            offline_claimed_at=datetime.combine(day, time(6)), full_body_file_id=f'photo-{uid}')])
                    for hotel, status in plans:
                        session.add(Shift(user_id=uid, hotel_id=hotel,
                            start_at=datetime.combine(day, time(6)), end_at=datetime.combine(day, time(15)), status=status))
                await session.commit()

            async def download(file_id, destination, timeout):
                destination.write(RAW)
            work.api.bot = SimpleNamespace(download=AsyncMock(side_effect=download))
            pending = json.loads((await attendance.pending(AttendanceRequest(admin))).text)
            assert {r['user_id'] for r in pending['items']} == {5, 10, 11}
            assert 'владелец' in pending['scopeNotice']
            pending_owner = json.loads((await attendance.pending(AttendanceRequest(owner))).text)
            assert {r['user_id'] for r in pending_owner['items']} == set(cases)
            assert pending_owner['scopeNotice'] == ''

            for uid, (day, _plans) in cases.items():
                evidence = AttendanceRequest(admin, purpose='start', id=str(uid))
                decision = {'miniapp_actor': admin, 'body': {
                    'userId': uid, 'date': str(day), 'purpose': 'start', 'approved': True,
                    'verifiedAt': datetime.combine(day, time(6), tzinfo=timezone.utc).isoformat(),
                    'note': 'Фото и время начала смены проверены'}}
                if uid in {5, 10, 11}:
                    assert (await attendance.evidence(evidence)).status == 200
                    assert (await attendance.review(decision)).status == 200
                else:
                    for operation in (attendance.evidence(evidence), attendance.review(decision)):
                        with pytest.raises(AccessError, match='Нет однозначной привязки') as error:
                            await operation
                        assert error.value.status == 403
                    # Owner retains access even without a uniquely scoped plan.
                    assert (await attendance.evidence(AttendanceRequest(owner, purpose='start', id=str(uid)))).status == 200
                    assert (await attendance.review(decision | {'miniapp_actor': owner})).status == 200
            async with factory() as session:
                assert all(r.status == 'STARTED' for r in await session.scalars(select(ShiftCheckIn)))
                assert list(await session.scalars(select(PayrollEntry))) == []
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_foreign_attendance_cannot_hide_own_hotel_after_the_first_page(monkeypatch):
    async def run():
        engine, factory, _work, attendance = await prepare(monkeypatch)
        try:
            async with factory() as session:
                session.add_all([User(id=4, tg_id=4, name='Admin'),
                    UserRole(user_id=4, role='ADMIN'), HotelEmployee(user_id=4, hotel_id=1)])
                # All older pages lack an authoritative hotel link.
                for number in range(202):
                    day = NOW.date()-timedelta(days=202-number)
                    session.add(ShiftCheckIn(user_id=1, shift_date=day, status='PENDING_REVIEW',
                        initiated_at=NOW.replace(tzinfo=None), location_received_at=NOW.replace(tzinfo=None),
                        offline_claimed_at=datetime.combine(day, time(6)), full_body_file_id='unlinked'))
                session.add_all([ShiftCheckIn(user_id=2, shift_date=NOW.date(), status='PENDING_REVIEW',
                    initiated_at=NOW.replace(tzinfo=None), location_received_at=NOW.replace(tzinfo=None),
                    offline_claimed_at=NOW.replace(tzinfo=None), full_body_file_id='owned'),
                    Shift(user_id=2, hotel_id=1, start_at=datetime.combine(NOW.date(), time(6)),
                        end_at=datetime.combine(NOW.date(), time(15)), status='PLANNED')])
                await session.commit()
            response = await attendance.pending(AttendanceRequest({'id': 4, 'roles': ['ADMIN']}))
            assert [r['user_id'] for r in json.loads(response.text)['items']] == [2]
        finally:
            await engine.dispose()
    asyncio.run(run())
