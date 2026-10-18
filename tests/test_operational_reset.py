import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import Base
from app.models import Booking, OperationalResetArchive
from app.operational_reset import (
    DELETE_TABLES,
    ResetError,
    inspect_archive,
    preview,
    reset,
    restore,
)
from app.schema_updates import upgrade


async def fixture():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    @event.listens_for(engine.sync_engine, 'connect')
    def foreign_keys(dbapi, _):
        cursor = dbapi.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.close()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await upgrade(conn)
        await conn.exec_driver_sql('ALTER TABLE bookings ADD COLUMN legacy_extra TEXT')
        tables = Base.metadata.tables
        async def put(table_name, **data):
            await conn.execute(tables[table_name].insert().values(**data))
        await put('users', id=1, tg_id=8000000001, name='Owner')
        await put('users', id=2, tg_id=8000000002, name='Photographer')
        await put('user_roles', user_id=1, role='OWNER')
        await put('user_roles', user_id=2, role='PHOTOGRAPHER')
        await put('hotels', id=1, name='Hotel')
        await put('hotel_employees', hotel_id=1, user_id=2)
        await put('clients', id=1, name='Guest', phone='+79991234567')
        await put('packages', id=1, name='Photo', price_per_photo=400)
        await put('bookings', id=10, hotel_id=1, client_id=1, room='22', guest_count=3,
                  shoot_date=date(2026, 10, 16), shoot_time=time(12, 13, 14, 500),
                  package_id=1, manager_id=1, photographer_id=2, status='COMPLETED')
        await conn.exec_driver_sql("UPDATE bookings SET legacy_extra='legacy-value' WHERE id=10")
        await put('shootings', id=11, booking_id=10)
        await put('photos', id=12, shooting_id=11, file_id='original-file')
        await put('photo_storage', photo_id=12, shooting_id=11, telegram_file_id='original-file',
                  telegram_unique_id='original-unique', source_kind='DOCUMENT', disk_path='/shoot/11/12.jpg')
        await put('photo_edits', photo_id=12, created_by_id=2, mode='AI')
        await put('shoot_development_reviews', shooting_id=11, photographer_id=2,
                  fingerprint='e' * 64, photo_ids='[12]')
        await put('sale_drafts', id=13, booking_id=10, created_by_id=2, status='COMPLETED',
                  expected_amount=Decimal('800.25'), sold_photos=2)
        await put('sale_draft_photos', draft_id=13, telegram_file_id='selected',
                  telegram_unique_id='selected-unique', storage_path='/selected/13/a.jpg',
                  sha256='a' * 64, byte_size=123)
        await put('payroll_entries', id=14, user_id=1, kind='Комиссия менеджера', amount=120.03,
                  period='2026-10', note='sale=15;booking=10')
        await put('payroll_entries', id=21, user_id=2, kind='Штраф за опоздание', amount=-500,
                  period='2026-10', note='late')
        await put('payroll_entries', id=22, user_id=2, kind='Ручной штраф', amount=-250,
                  period='2026-10')
        await put('payroll_entries', id=23, user_id=2, kind='Премия', amount=1000,
                  period='2026-10', note='Manual unrelated award')
        await put('payroll_entries', id=24, user_id=1, kind='Комиссия менеджера', amount=60,
                  period='2026-10', note='sale=9999;booking=9999')
        await put('sales', id=15, booking_id=10, created_by_id=2, credited_user_id=2,
                  source_draft_id=13, manager_payroll_entry_id=14, sold_photos=2,
                  amount=800.25, percent=15.0, commission=120.0375)
        await put('guest_feedback', sale_id=15, token_hash='t' * 64)
        await put('receipts', id=16, booking_id=10, uploaded_by_id=2, purpose='PAYMENT',
                  file_id='receipt-file', file_unique_id='receipt-unique', expected_amount=Decimal('800.25'),
                  verified_amount=Decimal('800.25'), status='APPROVED')
        await put('bank_reconciliations', receipt_id=16, bank_reference='bank-1', amount=Decimal('800.25'))
        await put('booking_reminders', booking_id=10, user_id=2, reminder_kind='2H')
        await put('repeat_sale_leads', booking_id=10, manager_id=1, offer_text='Offer')
        await put('delivery_galleries', id=17, booking_id=10, access_token='g' * 64,
                  title='Gallery', created_by_id=2)
        await put('delivery_photos', gallery_id=17, uploaded_by_id=2, filename='a.jpg',
                  disk_path='/selected/13/a.jpg', sha256='a' * 64, byte_size=123)
        await put('delivery_contacts', id=18, tg_id=9000000001, name='Guest')
        await put('delivery_claims', gallery_id=17, contact_id=18)
        await put('delivery_reminders', booking_id=10, contact_id=18, moment='2026-10-16', kind='2H')
        await put('delivery_booking_responses', booking_id=10, contact_id=18,
                  kind='CONFIRM', moment='2026-10-16')
        await put('delivery_campaigns', id=19, created_by_id=1, text='Campaign retained')
        await put('delivery_campaign_recipients', campaign_id=19, contact_id=18)
        await put('shifts', id=20, user_id=2, hotel_id=1,
                  start_at=datetime(2026, 10, 16, 8, tzinfo=UTC).replace(tzinfo=None),
                  end_at=datetime(2026, 10, 16, 20, tzinfo=UTC).replace(tzinfo=None))
        await put('shift_check_ins', user_id=2, shift_date=date(2026, 10, 16), late=True, fine_amount=500)
        await put('shift_check_outs', user_id=2, shift_date=date(2026, 10, 16), report_note='Report')
        await put('work_checklist_completions', user_id=2, shift_date=date(2026, 10, 16),
                  item_key='equipment', done=True)
        await put('compensation', user_id=2, role='PHOTOGRAPHER', sales_percent=15)
        await put('cash_movements', category='PAYROLL', amount=Decimal('100.25'),
                  paid_on=date(2026, 10, 16), employee_id=2, note='Paid', created_by_id=1)
        await put('notifications', user_id=1, text='Old sale', kind='control', event_key='sale:15:payment')
        await put('notifications', user_id=2, text='Academy retained', kind='academy', event_key='academy:1')
        await put('settings', key='PHOTO_PRICE', value='400')
        await put('settings', key='YANDEX_DISK_TOKEN', value='test-secret-preserved')
        await put('settings', key='gallery:meta:10', value='{"title":"Private title"}')
        await put('settings', key='booking-control:advice:1', value='Old advice')
        await put('settings', key='operations:maintenance', value='reset-request-0001')
        await put('operation_requests', user_id=2, request_key='sale-queued-old', payload_hash='q' * 64,
                  result='{"sale":15}')
        await put('audit_logs', user_id=1, action='existing-audit', details='Keep')
        await put('academy_reviews', id=25, user_id=2, photo_id=12, file_id='original-file',
                  composition_score=8, light_score=8, pose_score=8, emotion_score=8,
                  color_score=8, quality_score=8, recommendation='Keep this feedback')
        await put('training_assignments', id=26, user_id=2, assigned_date=date(2026, 10, 16),
                  category_slug='family', status='COMPLETED', ai_analysis='Keep learning')
        await put('training_submissions', assignment_id=26, pose_index=1,
                  reference_filename='ref.jpg', submitted_file_id='training-original')
        await put('academy_lesson_progress', user_id=2, topic_slug='light')
        await put('academy_practice_reviews', assignment_id=26, result='Keep', photos='[]')
        await put('hr_candidates', name='Candidate retained', role='PHOTOGRAPHER', created_by_id=1)
        await put('sales_training_sessions', user_id=2, client_type='family', transcript='[]')
        await put('work_chat_messages', sender_id=1, recipient_id=2, body='Keep chat')
        assert not (await conn.exec_driver_sql('PRAGMA foreign_key_check')).all()
    return engine


async def count(conn, name):
    return await conn.scalar(text(f'SELECT count(*) FROM {name}'))


def test_atomic_reset_complete_archive_typed_restore_and_preserved_domains():
    async def run():
        engine = await fixture()
        try:
            report = await preview(engine)
            assert all(report['row_counts'][name] > 0 for name in DELETE_TABLES)
            assert report['row_counts']['payroll_entries'] == 3
            async with engine.connect() as conn:
                assert await count(conn, 'operational_reset_archives') == 0
            result = await reset(engine, 'reset-request-0001', 1,
                                 expected_preview_sha256=report['snapshot_sha256'])
            assert not result['replayed']
            assert result['preserved_manifest']['shifts']['count'] == 1
            assert result['preserved_manifest']['users']['count'] == 2
            assert result['preserved_manifest']['payroll_entries']['count'] == 2
            assert (await preview(engine))['total_deleted_rows'] == 0
            assert await inspect_archive(engine, 'reset-request-0001') == {k: v for k, v in result.items() if k != 'replayed'}
            async with engine.connect() as conn:
                for name in DELETE_TABLES:
                    assert await count(conn, name) == 0
                for name in ('users', 'user_roles', 'hotel_employees', 'clients', 'packages',
                             'shifts', 'compensation', 'cash_movements', 'operation_requests',
                             'delivery_contacts', 'delivery_campaigns', 'delivery_campaign_recipients',
                             'training_assignments', 'training_submissions', 'academy_lesson_progress',
                             'academy_practice_reviews', 'hr_candidates', 'sales_training_sessions', 'work_chat_messages'):
                    assert await count(conn, name) > 0
                assert await conn.scalar(text('SELECT photo_id FROM academy_reviews WHERE id=25')) is None
                assert (await conn.execute(text('SELECT id FROM payroll_entries ORDER BY id'))).scalars().all() == [23, 24]
                assert await count(conn, 'notifications') == 1
                assert await conn.scalar(text("SELECT value FROM settings WHERE key='YANDEX_DISK_TOKEN'")) == 'test-secret-preserved'
                assert await conn.scalar(text("SELECT value FROM settings WHERE key='operations:maintenance'")) is None
                archive = (await conn.execute(select(OperationalResetArchive.__table__))).mappings().one()
                assert hashlib.sha256(archive['payload'].encode()).hexdigest() == archive['payload_sha256']
                assert json.loads(archive['row_counts']) == report['row_counts']
                assert not (await conn.exec_driver_sql('PRAGMA foreign_key_check')).all()
            restored = await restore(engine, 'reset-request-0001', 1)
            assert not restored['replayed'] and restored['generation'] != result['generation']
            assert (await restore(engine, 'reset-request-0001', 1))['replayed']
            async with engine.connect() as conn:
                assert await conn.scalar(text('SELECT legacy_extra FROM bookings WHERE id=10')) == 'legacy-value'
                assert await conn.scalar(text('SELECT photo_id FROM academy_reviews WHERE id=25')) == 12
                receipt = (await conn.execute(select(Base.metadata.tables['receipts']))).mappings().one()
                assert receipt['verified_amount'] == Decimal('800.25')
                booking = (await conn.execute(select(Booking.__table__))).mappings().one()
                assert booking['shoot_date'] == date(2026, 10, 16)
                assert booking['shoot_time'] == time(12, 13, 14, 500)
                assert not (await conn.exec_driver_sql('PRAGMA foreign_key_check')).all()
                assert await count(conn, 'audit_logs') == 3
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_reset_retry_never_erases_new_work_and_restore_refuses_new_work():
    async def run():
        engine = await fixture()
        try:
            original = await reset(engine, 'reset-request-0001', 1)
            async with engine.begin() as conn:
                await conn.execute(Booking.__table__.insert().values(id=100, hotel_id=1, client_id=1,
                    room='New', shoot_date=date(2026, 10, 18), shoot_time=time(15), package_id=1, manager_id=1))
            again = await reset(engine, 'reset-request-0001', 1)
            assert again['replayed'] and again['archive_sha256'] == original['archive_sha256']
            with pytest.raises(ResetError, match='New operational work'):
                await restore(engine, 'reset-request-0001', 1)
            async with engine.connect() as conn:
                assert await count(conn, 'bookings') == 1
                assert await count(conn, 'operational_reset_archives') == 1
                assert await count(conn, 'operational_reset_restores') == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_archive_is_immutable_and_unauthorized_or_stale_preview_cannot_reset():
    async def run():
        engine = await fixture()
        try:
            with pytest.raises(ResetError, match='active owner'):
                await reset(engine, 'reset-request-0001', 2)
            old = await preview(engine)
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE bookings SET room='Changed' WHERE id=10"))
            with pytest.raises(ResetError, match='changed after'):
                await reset(engine, 'reset-request-0001', 1, expected_preview_sha256=old['snapshot_sha256'])
            async with engine.connect() as conn:
                assert await count(conn, 'bookings') == 1
                assert await count(conn, 'operational_reset_archives') == 0
            await reset(engine, 'reset-request-0001', 1)
            for statement in ('UPDATE operational_reset_archives SET payload=\'{}\'',
                              'DELETE FROM operational_reset_archives'):
                with pytest.raises(IntegrityError, match='immutable'):
                    async with engine.begin() as conn:
                        await conn.exec_driver_sql(statement)
            assert (await inspect_archive(engine, 'reset-request-0001'))['archive_id'] == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_failure_mid_delete_rolls_back_both_archive_and_all_operational_data():
    async def run():
        engine = await fixture()
        try:
            before = await preview(engine)
            async with engine.begin() as conn:
                await conn.exec_driver_sql("""CREATE TRIGGER reject_booking_delete BEFORE DELETE ON bookings
                    BEGIN SELECT RAISE(ABORT, 'fixture forced failure'); END""")
            with pytest.raises(IntegrityError, match='fixture forced failure'):
                await reset(engine, 'reset-request-0001', 1)
            assert await preview(engine) == before
            async with engine.connect() as conn:
                assert await count(conn, 'operational_reset_archives') == 0
                assert await conn.scalar(text('SELECT photo_id FROM academy_reviews WHERE id=25')) == 12
                assert await conn.scalar(text("SELECT value FROM settings WHERE key='operations:generation'")) is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_unknown_foreign_key_dependency_refuses_reset_without_cascade_data_loss():
    async def run():
        engine = await fixture()
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql('CREATE TABLE unknown_booking_detail(id INTEGER PRIMARY KEY, booking_id INTEGER REFERENCES bookings(id) ON DELETE CASCADE)')
                await conn.exec_driver_sql('INSERT INTO unknown_booking_detail VALUES(1,10)')
            with pytest.raises(ResetError, match='Unreviewed dependency'):
                await reset(engine, 'reset-request-0001', 1)
            async with engine.connect() as conn:
                assert await count(conn, 'unknown_booking_detail') == 1
                assert await count(conn, 'bookings') == 1
                assert await count(conn, 'operational_reset_archives') == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_unintended_mutation_to_preserved_data_rolls_back_reset():
    async def run():
        engine = await fixture()
        try:
            before = await preview(engine)
            async with engine.begin() as conn:
                await conn.exec_driver_sql("""CREATE TRIGGER mutate_compensation AFTER DELETE ON sales
                    BEGIN UPDATE compensation SET sales_percent=0; END""")
            with pytest.raises(ResetError, match='Preservation verification failed for compensation'):
                await reset(engine, 'reset-request-0001', 1)
            assert await preview(engine) == before
            async with engine.connect() as conn:
                assert await count(conn, 'operational_reset_archives') == 0
                assert await conn.scalar(text('SELECT sales_percent FROM compensation')) == 15
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_generation_and_maintenance_guard_and_completed_replay_leave_other_reset_alone():
    async def run():
        engine = await fixture()
        try:
            with pytest.raises(ResetError, match='generation changed'):
                await reset(engine, 'reset-request-0001', 1, expected_generation='wrong-generation')
            with pytest.raises(ResetError, match='Another operational reset'):
                await reset(engine, 'reset-request-other', 1, expected_generation='')
            await reset(engine, 'reset-request-0001', 1, expected_generation='')
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO settings(key,value) VALUES('operations:maintenance','another-reset')"))
            assert (await reset(engine, 'reset-request-0001', 1, expected_generation=''))['replayed']
            async with engine.connect() as conn:
                assert await conn.scalar(text("SELECT value FROM settings WHERE key='operations:maintenance'")) == 'another-reset'
        finally:
            await engine.dispose()
    asyncio.run(run())
