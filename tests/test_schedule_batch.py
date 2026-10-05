"""One employee, multiple dates/hotels, conflict checks and atomic batch rollback."""
import json
import unittest
from datetime import timedelta

import test_miniapp_release as fixtures
from sqlalchemy import text
from test_miniapp_release import Request


class ScheduleBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.MiniAppTests()
        await self.fixture.asyncSetUp()
        self.api = self.fixture.service
        self.day = self.api.today()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def slot(self, hotel=1, start='09:00', end='12:00', offset=0):
        return {'hotelId': hotel, 'date': str(self.day+timedelta(days=offset)), 'start': start, 'end': end}

    async def call(self, slots, *, user=3, uid=1002):
        request = Request('/api/miniapp/schedule/batch', uid, 'POST', {'userId': user, 'slots': slots}, None)
        return await self.api.middleware(request, self.api.create_shift_batch)

    def count(self):
        with self.fixture.engine.inner.connect() as conn:
            return conn.execute(text('SELECT COUNT(*) FROM shifts')).scalar_one()

    async def test_two_hotels_same_day_and_repeated_days(self):
        response = await self.call([self.slot(), self.slot(2,'12:00','18:00'), self.slot(offset=1)])
        assert response.status == 201, response.text
        assert len(json.loads(response.text)['ids']) == self.count() == 3
        _, schedule, _ = await self.fixture.call(f'/schedule?from={self.day}&to={self.day+timedelta(days=1)}', uid=1003)
        assert [x['hotelId'] for x in schedule['items']] == [1,2,1]
        assert all(x['userId']==3 and x['attendance']=='PLANNED' for x in schedule['items'])

    async def test_internal_overlap_duplicate_and_existing_conflict_are_atomic(self):
        assert (await self.call([self.slot(), self.slot(2,'11:00','15:00')])).status == 409
        assert (await self.call([self.slot(), self.slot()])).status == 409
        assert self.count() == 0
        assert (await self.call([self.slot(offset=1)])).status == 201
        response = await self.call([self.slot(), self.slot(offset=1)])
        assert response.status == 409 and self.count() == 1

    async def test_inactive_hotel_and_bad_payload_roll_back(self):
        assert (await self.call([self.slot(), self.slot(hotel=999,offset=1)])).status == 400
        assert self.count() == 0
        assert (await self.call([self.slot() | {'userId': 5}])).status == 400
        assert (await self.call([self.slot()],user=True)).status == 400

    async def test_only_owner_admin_and_bounded_period(self):
        assert (await self.call([self.slot()],uid=1003)).status == 403
        assert (await self.call([self.slot()],uid=1004)).status == 403
        assert (await self.call([])).status == 400
        assert (await self.call([self.slot(offset=i) for i in range(63)])).status == 400
        assert (await self.call([self.slot(),self.slot(offset=31)])).status == 400
        assert self.count() == 0
        assert (await self.call([self.slot(),self.slot(offset=30)],uid=1001)).status == 201
