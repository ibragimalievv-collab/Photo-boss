"""One tap moves shifts planned before "09:00 until the last sale" to the rule."""
import json
import unittest
from datetime import datetime, time, timedelta, timezone

import test_miniapp_release as fixtures
from sqlalchemy import text
from test_miniapp_release import Request

from app.shift_hours import ShiftHours


class NormalizeHoursTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.MiniAppTests()
        await self.fixture.asyncSetUp()
        self.api = self.fixture.service
        self.today = self.api.today()
        self.tomorrow = self.today + timedelta(days=1)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def utc(self, day, hh, mm=0):
        local = datetime.combine(day, time(hh, mm), self.api.tz)
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    def shift(self, sid, uid, hid, day, start, end, status='PLANNED'):
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text('INSERT INTO shifts VALUES (:id,:uid,:hid,:st,:en,:status)'),
                         {'id': sid, 'uid': uid, 'hid': hid, 'st': self.utc(day, *start),
                          'en': self.utc(day, *end), 'status': status})

    def hours(self, sid):
        with self.fixture.engine.inner.connect() as conn:
            row = conn.execute(text('SELECT start_at,end_at FROM shifts WHERE id=:id'), {'id': sid}).one()
        local = lambda raw: datetime.fromisoformat(str(raw)).replace(tzinfo=timezone.utc).astimezone(self.api.tz)
        return local(row[0]).strftime('%H:%M'), local(row[1]).strftime('%H:%M')

    async def normalize(self, uid=1001, body=None):
        request = Request('/api/miniapp/schedule/normalize-hours', uid, 'POST',
                          {'confirm': True} if body is None else body, None)
        response = await self.api.middleware(request, ShiftHours(self.api).normalize)
        return response.status, json.loads(response.text)

    async def legacy(self, uid=1001):
        status, data, _ = await self.fixture.call(f'/schedule?from={self.today}&to={self.today}', uid=uid)
        assert status == 200, data
        return data['legacyHours']

    def prepare_owner_week(self):
        self.shift(10, 3, 1, self.tomorrow, (9, 0), (19, 0))                         # old hours
        self.shift(11, 4, 2, self.tomorrow + timedelta(days=1), (10, 0), (18, 0))    # old hours
        self.shift(12, 5, 1, self.tomorrow, (9, 0), (12, 0))                         # two shifts
        self.shift(13, 5, 2, self.tomorrow, (12, 0), (18, 0))                        #   on one day
        self.shift(14, 3, 1, self.today, (9, 0), (19, 0))                            # today: never
        self.shift(15, 4, 1, self.tomorrow + timedelta(days=2), (9, 0), (23, 59))    # already the rule
        self.shift(16, 3, 1, self.tomorrow + timedelta(days=3), (9, 0), (19, 0), 'CANCELLED')
        plan = {'userId': 3, 'date': str(self.tomorrow), 'role': 'MANAGER', 'hotelId': 1,
                'start': '09:00', 'end': '19:00'}
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text('INSERT INTO settings VALUES (:key,:value)'),
                         {'key': f'schedule.day:{self.tomorrow}:3', 'value': json.dumps(plan)})

    async def test_owner_moves_future_shifts_once_and_keeps_today_conflicts_and_role(self):
        self.prepare_owner_week()
        legacy = await self.legacy()
        assert (legacy['count'], legacy['from'], legacy['to'], legacy['conflictCount']) == (
            2, str(self.tomorrow), str(self.tomorrow + timedelta(days=1)), 1)
        assert legacy['conflicts'] == [{'userId': 5, 'date': str(self.tomorrow)}]
        assert (await self.normalize(body={}))[0] == 400
        assert self.hours(10) == ('09:00', '19:00')

        status, result = await self.normalize()
        assert status == 200 and result['changed'] == 2 and result['conflictCount'] == 1, result
        assert self.hours(10) == self.hours(11) == ('09:00', '23:59')
        assert self.hours(12) == ('09:00', '12:00') and self.hours(13) == ('12:00', '18:00')
        assert self.hours(14) == ('09:00', '19:00')
        assert self.hours(15) == ('09:00', '23:59') and self.hours(16) == ('09:00', '19:00')
        with self.fixture.engine.inner.connect() as conn:
            plan = json.loads(conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                                           {'key': f'schedule.day:{self.tomorrow}:3'}).scalar_one())
            audits = conn.execute(text("SELECT entity_id,details FROM audit_logs "
                                       "WHERE action='miniapp_shift_hours_normalized' ORDER BY entity_id")).all()
        assert (plan['start'], plan['end'], plan['role']) == ('09:00', '23:59', 'MANAGER')
        assert [a[0] for a in audits] == [10, 11] and '09:00–19:00 → с 09:00 до последней продажи' in audits[0][1]
        # The imported role still labels the shift; no second "plan only" row.
        _, day, _ = await self.fixture.call(f'/schedule?from={self.tomorrow}&to={self.tomorrow}', uid=1001)
        own = [x for x in day['items'] if x['userId'] == 3]
        assert len(own) == 1 and own[0]['role'] == 'MANAGER' and own[0]['importedPlan'], own

        status, again = await self.normalize()
        assert status == 200 and again['changed'] == 0
        legacy = await self.legacy()
        assert legacy['count'] == 0 and legacy['conflictCount'] == 1

    async def test_admin_moves_only_own_hotels_and_sees_cross_hotel_conflicts(self):
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text('INSERT INTO hotel_employees VALUES (2,1)'))
        self.shift(20, 3, 1, self.tomorrow, (10, 0), (19, 0))      # hotel 1: admin's
        self.shift(21, 4, 2, self.tomorrow, (10, 0), (19, 0))      # hotel 2: not admin's
        self.shift(22, 5, 1, self.tomorrow, (9, 0), (12, 0))       # same person, the second
        self.shift(23, 5, 2, self.tomorrow, (12, 0), (18, 0))      #   shift is in hotel 2
        legacy = await self.legacy(uid=1002)
        assert legacy['count'] == 1 and legacy['conflicts'] == [{'userId': 5, 'date': str(self.tomorrow)}]
        status, result = await self.normalize(uid=1002)
        assert status == 200 and result['changed'] == 1
        assert self.hours(20) == ('09:00', '23:59')
        assert self.hours(21) == ('10:00', '19:00')
        assert self.hours(22) == ('09:00', '12:00') and self.hours(23) == ('12:00', '18:00')

    async def test_only_schedule_editors(self):
        self.shift(30, 3, 1, self.tomorrow, (9, 0), (19, 0))
        assert await self.legacy(uid=1003) is None
        assert (await self.normalize(uid=1003))[0] == 403
        assert (await self.normalize(uid=1004))[0] == 403
        assert self.hours(30) == ('09:00', '19:00')


if __name__ == '__main__':
    unittest.main()
