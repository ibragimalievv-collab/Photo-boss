"""Release-blocking regressions: disk acknowledgement and hotel finance boundaries.

Synthetic fixtures only. These tests never send photos, money or messages.
"""
import asyncio
import hashlib
import unittest
from datetime import timedelta

import pytest
import test_miniapp_release as baseline
from sqlalchemy import func, select, text
from test_sales_followups import prepared

from app.models import AuditLog, Photo, PhotoStorage, Sale, Shooting, User, utc_now
from app.services.photo_storage import StorageConfirmationPending
from app.services.sale_workflow import complete_full_upload
from app.yandex_disk import ROOT


async def confirm_photos(session, shooting):
    photos = list((await session.scalars(select(Photo).where(Photo.shooting_id == shooting.id))).all())
    records = []
    for photo in photos:
        row = PhotoStorage(photo_id=photo.id, shooting_id=shooting.id,
            telegram_file_id=photo.file_id, telegram_unique_id=f'test-{photo.id}',
            source_kind='DOCUMENT', status='STORED', byte_size=1000,
            sha256=hashlib.sha256(f'original-{photo.id}'.encode()).hexdigest(),
            disk_path=ROOT + f'/shootings/{shooting.id}/{photo.id}.jpg', stored_at=utc_now())
        session.add(row)
        records.append(row)
    await session.flush()
    return records


@pytest.mark.parametrize('failure', ['missing_all', 'missing_one', 'PENDING', 'UPLOADING', 'FAILED',
    'empty_path', 'wrong_path', 'empty_hash', 'bad_hash', 'empty_size', 'empty_time', 'wrong_shoot'])
def test_incomplete_storage_never_closes_upload_or_finalizes_commission(failure):
    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                shooting.full_upload_completed_at = None
                actor = await session.get(User, 2)
                if failure != 'missing_all':
                    rows = await confirm_photos(session, shooting)
                    last = rows[-1]
                    if failure == 'missing_one':
                        await session.delete(last)
                    elif failure in {'PENDING', 'UPLOADING', 'FAILED'}:
                        last.status = failure
                    elif failure == 'empty_path': last.disk_path = None
                    elif failure == 'wrong_path': last.disk_path = ROOT + '/shootings/99999/photo.jpg'
                    elif failure == 'empty_hash': last.sha256 = None
                    elif failure == 'bad_hash': last.sha256 = 'not-a-hash'
                    elif failure == 'empty_size': last.byte_size = 0
                    elif failure == 'empty_time': last.stored_at = None
                    elif failure == 'wrong_shoot': last.shooting_id = shooting.id + 999
                sale = Sale(booking_id=bid, created_by_id=1, credited_user_id=2,
                    sold_photos=1, declared_photo_count=10, amount=400, percent=0, commission=0)
                session.add(sale)
                await session.flush()
                with pytest.raises(StorageConfirmationPending, match='не завершена'):
                    await complete_full_upload(session, actor, shooting.id)
                assert shooting.full_upload_completed_at is None
                assert sale.commission_finalized_at is None and sale.commission == 0
                assert not await session.scalar(select(AuditLog.id).where(AuditLog.action == 'full_shoot_upload_completed'))
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_ready_storage_closes_once_and_keeps_percentage_boundary():
    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                shooting.full_upload_completed_at = None
                # 150 independently confirmed files, not just 150 DB rows.
                session.add_all(Photo(shooting_id=shooting.id, file_id=f'extra-{i}') for i in range(140))
                await session.flush()
                await confirm_photos(session, shooting)
                sale = Sale(booking_id=bid, created_by_id=1, credited_user_id=2,
                    sold_photos=1, declared_photo_count=150, amount=400, percent=0, commission=0)
                session.add(sale)
                await session.flush()
                result = await complete_full_upload(session, await session.get(User, 2), shooting.id)
                assert result.count == 150 and sale.percent == 15 and sale.commission == 60
                assert shooting.full_upload_completed_at and sale.commission_finalized_at
                await session.flush()
                with pytest.raises(ValueError, match='закрыта'):
                    await complete_full_upload(session, await session.get(User, 2), shooting.id)
                assert await session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == 'full_shoot_upload_completed')) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_exact_duplicate_originals_do_not_raise_photographer_percentage():
    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                shooting.full_upload_completed_at = None
                rows = await confirm_photos(session, shooting)
                rows[-1].sha256 = rows[0].sha256
                await session.flush()
                with pytest.raises(ValueError, match='точные копии'):
                    await complete_full_upload(session, await session.get(User, 2), shooting.id)
                assert shooting.full_upload_completed_at is None
        finally:
            await engine.dispose()
    asyncio.run(run())


class HotelFinanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = baseline.MiniAppTests.asyncSetUp
    asyncTearDown = baseline.MiniAppTests.asyncTearDown
    call = baseline.MiniAppTests.call

    def grants_and_entries(self):
        with self.engine.inner.begin() as conn:
            conn.execute(text('INSERT INTO hotel_employees VALUES(2,1),(4,1),(4,2)'))
            # Same manager works in both hotels: membership is not a payroll scope.
            conn.execute(text("INSERT INTO payroll_entries VALUES(3,4,'Комиссия менеджера',3150,:now),(4,4,'Комиссия менеджера',3900,:now),(5,4,'Ручная премия',7777,:now)"), {'now': utc_now()})
            conn.execute(text('UPDATE sales SET manager_payroll_entry_id=CASE WHEN id=1 THEN 3 ELSE 4 END'))
            conn.execute(text("UPDATE receipts SET status='APPROVED' WHERE id=2"))

    async def test_admin_sees_only_granted_hotel_in_rows_totals_cash_payroll_dashboard(self):
        self.grants_and_entries()
        status, data, _ = await self.call('/finance', uid=1002)
        assert status == 200, data
        assert data['scope'] == 'hotels' and data['unscopedAdjustmentsExcluded']
        assert [s['bookingId'] for s in data['salesRows']] == [1]
        assert data['salesCount'] == 1 and data['sales'] == 2100000
        assert data['cashReceived'] == 2100000
        assert data['payroll'] == 630000  # 3150 photographer + explicitly linked 3150 manager.
        assert {e['id'] for e in data['employees']} == {3, 4}
        assert all(e['adjustments'] != 777700 for e in data['employees'])
        status, dashboard, _ = await self.call('/dashboard', uid=1002)
        assert status == 200 and dashboard['finance'] == data

    async def test_no_hotels_gives_empty_report_and_revocation_is_immediate(self):
        self.grants_and_entries()
        assert (await self.call('/finance', uid=1002))[1]['salesCount'] == 1
        with self.engine.inner.begin() as conn:
            conn.execute(text('DELETE FROM hotel_employees WHERE user_id=2'))
        status, data, _ = await self.call('/finance', uid=1002)
        assert status == 200 and data['salesRows'] == [] and data['employees'] == []
        assert data['sales'] == data['cashReceived'] == data['payroll'] == 0

    async def test_owner_and_employee_keep_unscoped_adjustments(self):
        self.grants_and_entries()
        status, owner, _ = await self.call('/finance', uid=1001)
        assert status == 200 and owner['sales'] == owner['cashReceived'] == 4700000
        assert not owner['unscopedAdjustmentsExcluded'] and owner['scope'] == 'company'
        status, employee, _ = await self.call('/finance', uid=1004)
        assert status == 200 and employee['scope'] == 'self'
        assert employee['payroll'] == (3150 + 3900 + 7777) * 100
        assert employee['cashReceived'] is None

    async def test_permissions_union_does_not_override_admin_hotel_scope(self):
        self.grants_and_entries()
        with self.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO user_roles VALUES (2,'MANAGER')"))
            conn.execute(text('UPDATE bookings SET manager_id=2 WHERE id=2'))
        assert [s['bookingId'] for s in (await self.call('/finance', uid=1002))[1]['salesRows']] == [1]

    async def test_payroll_from_prior_sale_uses_sale_hotel_not_sale_report_date(self):
        self.grants_and_entries()
        with self.engine.inner.begin() as conn:
            conn.execute(text('UPDATE sales SET created_at=:old WHERE id=1'), {'old': utc_now() - timedelta(days=3)})
        status, data, _ = await self.call('/finance', uid=1002)
        assert status == 200 and data['sales'] == 0
        assert data['payroll'] == 315000
        assert data['cashReceived'] == 2100000


def test_missing_storage_jobs_can_be_repaired_without_deleting_or_duplicating_photos():
    from app.services.photo_storage import repair_storage_queue

    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                shooting.full_upload_completed_at = None
                before = list((await session.scalars(select(Photo.id))).all())
                assert await repair_storage_queue(session, shooting.id) == {'enqueued': 10, 'retried': 0}
                assert await repair_storage_queue(session, shooting.id) == {'enqueued': 0, 'retried': 0}
                assert list((await session.scalars(select(Photo.id))).all()) == before
                rows = list((await session.scalars(select(PhotoStorage).order_by(PhotoStorage.id))).all())
                rows[0].status = 'FAILED'
                rows[0].attempts = 5
                rows[1].status = 'UPLOADING'
                rows[1].attempts = 2
                rows[1].started_at = utc_now()
                await session.flush()
                assert await repair_storage_queue(session, shooting.id) == {'enqueued': 0, 'retried': 1}
                assert rows[0].status == 'PENDING' and rows[0].attempts == 0
                assert rows[1].status == 'UPLOADING' and rows[1].attempts == 2
                with pytest.raises(StorageConfirmationPending):
                    await complete_full_upload(session, await session.get(User, 2), shooting.id)
                assert shooting.full_upload_completed_at is None
                assert list((await session.scalars(select(Photo.id))).all()) == before
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_workflow_reports_pending_disk_as_retryable_dependency():
    from types import SimpleNamespace

    from app.miniapp_security import AccessError
    from app.workflow import Workflow

    async def run():
        engine, factory, bid = await prepared()
        try:
            async with factory() as session:
                shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == bid))
                shooting.full_upload_completed_at = None
                sid = shooting.id
                await session.commit()
            service = Workflow(SimpleNamespace())
            async with engine.begin() as conn:
                with pytest.raises(AccessError) as error:
                    await service.apply(conn, {'id': 2, 'roles': ['PHOTOGRAPHER']}, 'shoot_complete', {'shooting': {'id': sid}})
                assert error.value.status == 424
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_missing_file_chunk_stops_instead_of_spinning_forever():
    from unittest.mock import AsyncMock

    from test_media_recovery import Part, Request

    from app.miniapp_security import AccessError
    from app.workday import Workday

    class InterruptedPart(Part):
        def at_eof(self):
            return False

        async def read_chunk(self, _size):
            return b''

    async def run():
        service = Workday(None)
        service.execute = AsyncMock()
        request = Request([Part('operation', [b'{}']), InterruptedPart('file', [])])
        with pytest.raises(AccessError) as error:
            await asyncio.wait_for(service.media(request), 1)
        assert error.value.status == 503
        service.execute.assert_not_awaited()
    asyncio.run(run())
