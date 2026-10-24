"""Full text schedule contract; isolated database, never production identities."""
import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import test_miniapp_release as fixtures
from sqlalchemy import text
from test_miniapp_release import Request

from app.miniapp_security import AccessError
from app.schedule_import import ScheduleImport
from app.schedule_text import parse_schedule

SOURCE = (Path(__file__).parent / 'fixtures/lisa_schedule_20261022.txt').read_text()


def test_lisa_dates_and_counts():
    value = parse_schedule(SOURCE)
    assert value['counts'] == {'MANAGER': 15, 'PHOTOGRAPHER': 11, 'OFF': 5, 'total': 31}
    assert value['from'] == '2026-10-22' and value['to'] == '2026-11-21'
    tail = value['days'][9:]
    assert {r: sum(d['role'] == r for d in tail) for r in ('MANAGER', 'PHOTOGRAPHER', 'OFF')} == {
        'MANAGER': 12, 'PHOTOGRAPHER': 8, 'OFF': 2}
    assert value['days'][9] == {'date': '2026-10-31', 'role': 'MANAGER'}


@pytest.mark.parametrize('source', [
    '', '22.10 — менеджер', '31.02.2026 — фотограф',
    '22.10.2026 — менеджер\n22.10.2026 — фотограф',
    '22.10.2026 — менеджер\n24.10.2026 — фотограф',
    '25.10.2026–22.10.2026 — фотограф',
    '22.10.2026–22.11.2026 — менеджер',
    'Период: 22.10.2026–23.10.2026\n22.10.2026 — менеджер',
    '22.10.2026 — менеджер\nМенеджер — 2 дня',
    '22.10.2026 — администратор',
    '22.10.2026 — менеджер\nИзменить всех сотрудников',
])
def test_parser_fails_closed(source):
    with pytest.raises(AccessError):
        parse_schedule(source)


def test_parser_year_boundary_and_single_day():
    assert parse_schedule('31.12.2026–01.01.2027 — выходные.')['counts']['OFF'] == 2
    assert parse_schedule('01.01.2027: Фотографом!')['counts']['PHOTOGRAPHER'] == 1


class ScheduleTextImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.MiniAppTests()
        await self.fixture.asyncSetUp()
        self.api = self.fixture.service
        self.api.today = lambda: date(2026, 10, 24)
        self.service = ScheduleImport(self.api)
        self.body = {'userId': 3, 'text': SOURCE,
                     'defaults': {'hotelId': 1, 'start': '09:00', 'end': '19:00'}}
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text('INSERT INTO hotel_employees VALUES (2,1)'))

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def call(self, action, body=None, uid=1001):
        request = Request('/api/miniapp/schedule/text/'+action, uid, 'POST', body or self.body, None)
        response = await self.api.middleware(request, getattr(self.service, action))
        return response.status, json.loads(response.text)

    def rows(self, query):
        with self.fixture.engine.inner.connect() as conn:
            return [dict(r) for r in conn.execute(text(query)).mappings()]

    async def preview(self, body=None, uid=1001):
        status, result = await self.call('preview', body, uid)
        assert status == 200, result
        return result

    async def apply(self, body=None, uid=1001, key='schedule-test-request-0001'):
        body = body or self.body
        preview = await self.preview(body, uid)
        assert preview['canApply'], preview
        status, result = await self.call('apply', {**body, 'revision': preview['revision'], 'requestKey': key}, uid)
        return status, result

    async def test_apply_all_31_days_then_reread_roles_and_days_off(self):
        untouched = {table: self.rows(f'SELECT * FROM {table}') for table in (
            'user_roles','sales','payroll_entries','bookings','shift_check_ins','shift_check_outs','work_breaks')}
        status, result = await self.apply()
        assert status == 200 and result['verified'], result
        assert result['counts'] == parse_schedule(SOURCE)['counts']
        assert len(result['days']) == 31
        status, verified = await self.call('verify', {'requestKey': 'schedule-test-request-0001'})
        assert status == 200 and result == verified
        for table, expected in untouched.items():
            assert self.rows(f'SELECT * FROM {table}') == expected
        status, calendar, _ = await self.fixture.call('/schedule?from=2026-10-22&to=2026-11-21', uid=1003)
        assert status == 200 and len(calendar['items']) == 31
        assert sum(d['role'] == 'OFF' for d in calendar['items']) == 5
        assert sum(d['role'] == 'MANAGER' for d in calendar['items']) == 15
        assert sum(d['role'] == 'PHOTOGRAPHER' for d in calendar['items']) == 11
        assert all(d['userId'] == 3 for d in calendar['items'])

    async def test_no_defaults_does_not_invent_hotel_or_hours(self):
        result = await self.preview({'userId': 3, 'text': SOURCE})
        assert result['needs'] == ['end', 'hotelId', 'start'] and not result['canApply']
        assert not self.rows('SELECT * FROM shifts')
        assert not self.rows('SELECT * FROM settings')
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text('INSERT INTO hotel_employees VALUES (3,1)'))
        result = await self.preview({'userId': 3, 'text': SOURCE})
        assert result['needs'] == ['end', 'start']

    async def test_duplicate_retry_same_key_and_new_key_no_duplicate_shifts(self):
        preview = await self.preview()
        body = {**self.body, 'revision': preview['revision'], 'requestKey': 'retry-request-key-0001'}
        assert (await self.call('apply', body))[0] == 200
        first = self.rows('SELECT * FROM shifts ORDER BY id')
        assert (await self.call('apply', body))[0] == 200
        assert self.rows('SELECT * FROM shifts ORDER BY id') == first
        assert (await self.apply(key='different-request-key-002'))[0] == 200
        assert self.rows('SELECT * FROM shifts ORDER BY id') == first
        other = {**body, 'text': body['text'].replace('ГРАФИК ЛИЗЫ', 'ГРАФИК ДРУГОЙ')}
        assert (await self.call('apply', other))[0] == 409

    async def test_history_other_employee_outside_period_and_attendance_preserved(self):
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO shifts VALUES (100,3,1,'2026-10-22 05:00:00','2026-10-22 12:00:00','PLANNED'),"
                              "(101,3,1,'2026-10-24 05:00:00','2026-10-24 12:00:00','PLANNED'),"
                              "(102,5,2,'2026-10-25 05:00:00','2026-10-25 12:00:00','PLANNED'),"
                              "(103,3,2,'2026-11-22 05:00:00','2026-11-22 12:00:00','PLANNED'),"
                              "(104,3,1,'2026-10-28 05:00:00','2026-10-28 12:00:00','PLANNED')"))
            conn.execute(text("INSERT INTO shift_check_ins VALUES (100,3,'2026-10-24','2026-10-24 06:01:00')"))
        old = self.rows('SELECT * FROM shifts WHERE id BETWEEN 100 AND 103 ORDER BY id')
        status, data = await self.apply()
        assert status == 200 and '2026-10-24' in data['preservedDays'], data
        assert self.rows('SELECT * FROM shifts WHERE id BETWEEN 100 AND 103 ORDER BY id') == old
        assert self.rows('SELECT status FROM shifts WHERE id=104') == [{'status': 'CANCELLED'}]
        assert len(self.rows('SELECT * FROM shift_check_ins')) == 1

    async def test_server_permissions_and_other_hotel_are_checked(self):
        for uid in (1003, 1004):
            assert (await self.call('preview', self.body, uid))[0] == 403
        assert (await self.apply(uid=1002))[0] == 200
        other = {**self.body, 'defaults': {**self.body['defaults'], 'hotelId': 2}}
        assert (await self.call('preview', other, 1002))[0] == 403
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO shifts (user_id,hotel_id,start_at,end_at,status) VALUES"
                              "(3,2,'2026-11-10 06:00:00','2026-11-10 16:00:00','PLANNED')"))
        assert (await self.call('preview', self.body, 1002))[0] == 403

    async def test_revision_changes_abort_before_writes(self):
        preview = await self.preview()
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO shifts VALUES (100,3,1,'2026-10-25 05:00:00','2026-10-25 12:00:00','PLANNED')"))
        snapshot = self.rows('SELECT * FROM shifts')
        status, _ = await self.call('apply', {**self.body, 'revision': preview['revision'], 'requestKey': 'changed-revision-key-0001'})
        assert status == 409
        assert self.rows('SELECT * FROM shifts') == snapshot
        assert not self.rows('SELECT * FROM settings')

    async def test_failure_rolls_back_entire_period_and_receipt(self):
        preview = await self.preview()
        with patch.object(self.api, 'audit_write', new=AsyncMock(side_effect=RuntimeError('test injected database failure'))):
            status, _ = await self.call('apply', {**self.body, 'revision': preview['revision'], 'requestKey': 'transaction-test-key-001'})
        assert status == 503
        assert not self.rows('SELECT * FROM shifts')
        assert not self.rows('SELECT * FROM settings')

    async def test_manual_change_invalidates_receipt_and_daily_annotation(self):
        assert (await self.apply())[0] == 200
        slot = self.rows("SELECT id FROM shifts WHERE start_at>'2026-11-20' ORDER BY start_at LIMIT 1")[0]['id']
        status, _, _ = await self.fixture.call(f'/schedule/{slot}', method='DELETE')
        assert status == 200
        assert (await self.call('verify', {'requestKey': 'schedule-test-request-0001'}))[0] == 409

    async def test_confirmed_existing_single_template_is_reused(self):
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO shifts VALUES (100,3,1,'2026-10-22 06:00:00','2026-10-22 16:00:00','PLANNED')"))
        body = {'userId': 3, 'text': SOURCE}
        preview = await self.preview(body)
        assert preview['canApply']
        assert all(d['hotelId'] == 1 for d in preview['days'])
        assert all(d['start'] == '09:00' and d['end'] == '19:00' for d in preview['days'] if d['role'] != 'OFF')
        assert (await self.apply(body))[0] == 200

    async def test_startup_requires_unique_confirmed_employee_and_existing_settings(self):
        from app.schedule_import_startup import (
            PRODUCTION_SERVICE,
            startup_schedule_import,
        )
        request = {'employeeName': 'Лиза', 'text': SOURCE, 'requestKey': 'startup-schedule-test-0001'}
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text("UPDATE users SET name='Лиза' WHERE id=3"))
        result = await startup_schedule_import(self.service, json.dumps(request), [1001], service_id=PRODUCTION_SERVICE)
        assert result['status'] == 'needs_confirmation' and result['reason'] == 'settings'
        assert not self.rows('SELECT * FROM settings')
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text("UPDATE users SET name='Лиза' WHERE id=4"))
        result = await startup_schedule_import(self.service, json.dumps(request), [1001], service_id=PRODUCTION_SERVICE)
        assert result['status'] == 'needs_confirmation' and len(result['candidates']) == 2
        assert not self.rows('SELECT * FROM settings')

    async def test_startup_verified_once_then_never_overwrites_later_manual_edit(self):
        from app.schedule_import_startup import (
            PRODUCTION_SERVICE,
            startup_schedule_import,
        )
        request = {**self.body, 'requestKey': 'startup-schedule-test-0001'}
        result = await startup_schedule_import(self.service, json.dumps(request), [1001], service_id='unrelated')
        assert result['status'] == 'blocked' and not self.rows('SELECT * FROM shifts')
        result = await startup_schedule_import(self.service, json.dumps(request), [999], service_id=PRODUCTION_SERVICE)
        assert result['status'] == 'blocked' and not self.rows('SELECT * FROM shifts')
        result = await startup_schedule_import(self.service, json.dumps(request), [1001], service_id=PRODUCTION_SERVICE)
        assert result['status'] == 'verified' and result['counts']['total'] == 31
        saved = self.rows('SELECT * FROM shifts ORDER BY id')
        assert (await startup_schedule_import(self.service, json.dumps(request), [1001], service_id=PRODUCTION_SERVICE))['status'] == 'verified'
        assert self.rows('SELECT * FROM shifts ORDER BY id') == saved
        with self.fixture.engine.inner.begin() as conn:
            conn.execute(text("DELETE FROM settings WHERE key='schedule.day:2026-11-20:3'"))
        result = await startup_schedule_import(self.service, json.dumps(request), [1001], service_id=PRODUCTION_SERVICE)
        assert result['status'] == 'blocked'
        assert not self.rows("SELECT * FROM settings WHERE key='schedule.day:2026-11-20:3'")
