"""Run additive migrations and audit checks in a disposable PostgreSQL schema."""
import asyncio
import json
import os
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit_capture import TABLES, install_capture
from app.audit_context import actor_id, install_actor_context
from app.db import Base
from app.models import (
    AuditLog,
    Booking,
    CashMovement,
    Hotel,
    Notification,
    OperationRequest,
    Package,
    Photo,
    Sale,
    ShootDevelopmentReview,
    Shooting,
    User,
    UserRole,
)
from app.schema_updates import upgrade
from app.workday import Workday


async def main():
    # This separate variable is set only for the disposable CI database.
    url = os.environ['UPDATE_POSTGRES_TEST_URL']
    schema = 'pb_update_test_' + uuid.uuid4().hex
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    install_actor_context(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
            # Reconstruct the pre-update surface without touching other schemas.
            for table in ('hr_candidates', 'academy_assessments', 'work_checklist_completions',
                          'operation_requests', 'guest_feedback', 'shoot_development_reviews',
                          'sales_training_sessions', 'cash_movements', 'photo_edits'):
                await conn.execute(text(f'DROP TABLE {table}'))
            for column in ('manager_percent_applied','manager_payroll_entry_id'):
                await conn.execute(text(f'ALTER TABLE sales DROP COLUMN {column}'))
            for column in ('event_key', 'priority', 'kind', 'payload', 'acknowledged_at', 'resolved_at'):
                await conn.execute(text(f'ALTER TABLE notifications DROP COLUMN {column}'))
            for table in ('shift_check_ins', 'shift_check_outs'):
                await conn.execute(text(f'ALTER TABLE {table} DROP COLUMN offline_claimed_at'))
            for column in ('report_note', 'report_saved_at'):
                await conn.execute(text(f'ALTER TABLE shift_check_outs DROP COLUMN {column}'))
            await conn.execute(text("INSERT INTO users(id,tg_id,name,active,created_at) VALUES (1,9876543210,'Before',true,CURRENT_TIMESTAMP)"))
            await conn.execute(text("INSERT INTO notifications(user_id,text,sent,created_at) VALUES (1,'Legacy notification',true,CURRENT_TIMESTAMP)"))
            await conn.execute(text("INSERT INTO sales(id,created_by_id,credited_user_id,commission_role,sold_photos,amount,percent,commission,payment_status,created_at) VALUES (999,1,1,'PHOTOGRAPHER',3,1234.56,12.5,154.32,'PAID','2026-09-01 10:00:00')"))
            await conn.execute(text("INSERT INTO payroll_entries(id,user_id,kind,amount,period,note,created_at) VALUES (999,1,'Legacy bonus',41.25,'2026-09','Legacy note','2026-09-01 10:00:00')"))
            await conn.execute(text("INSERT INTO shift_check_ins(id,user_id,shift_date,status,initiated_at,started_at,late,fine_amount) VALUES (999,1,'2026-09-01','STARTED','2026-09-01 06:00:00','2026-09-01 06:00:00',false,0)"))
            legacy_money={table:await conn.scalar(text(f'SELECT to_jsonb(t) FROM {table} t WHERE id=999')) for table in ('sales','payroll_entries','shift_check_ins')}


        migrations = sorted(p for p in Path('migrations').glob('*.sql') if 4<=int(p.name.split('_')[0])<=12)
        assert len(migrations) == 9
        for _ in range(2):
            async with engine.begin() as conn:
                driver = (await conn.get_raw_connection()).driver_connection
                for migration in migrations:
                    await driver.execute(migration.read_text())
                await upgrade(conn)
                await install_capture(conn)

        async with factory() as session:
            legacy = await session.scalar(select(Notification))
            assert legacy.text == 'Legacy notification' and legacy.sent
            assert legacy.kind == 'legacy' and legacy.priority == 'info'
            assert (await session.get(User, 1)).tg_id == 9876543210
            for table,original in legacy_money.items():
                current=await session.scalar(text(f'SELECT to_jsonb(t) FROM {table} t WHERE id=999'))
                assert {k:current[k] for k in original}==original, f'Legacy ledger changed: {table}'
            sale=await session.get(Sale,999)
            assert sale.manager_percent_applied is None and sale.manager_payroll_entry_id is None


        token = actor_id.set(1)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE users SET name='After' WHERE id=1"))
            async with factory() as session:
                entry = await session.scalar(select(AuditLog).where(AuditLog.action == 'row.update'))
                assert entry.user_id == 1
                details = json.loads(entry.details)
                assert details['before']['name'] == 'Before' and details['after']['name'] == 'After'
                user = await session.get(User, 1)
                user.name = 'Rolled back'
                await session.flush()
                await session.rollback()
                entries = (await session.scalars(select(AuditLog).where(AuditLog.action == 'row.update'))).all()
                assert len(entries) == 1
        finally:
            actor_id.reset(token)

        async with engine.begin() as conn:
            await conn.execute(text("UPDATE users SET name='Background' WHERE id=1"))
            entry = (await conn.execute(text("SELECT user_id FROM audit_logs WHERE action='row.update' ORDER BY id DESC LIMIT 1"))).one()
            assert entry.user_id is None, 'Actor leaked between transactions'
            triggers = await conn.scalar(text("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND t.tgname LIKE 'pb_audit_%'"), {'schema': schema})
            assert triggers == len(TABLES)
        print('PostgreSQL migrations 004–012 twice, legacy data, audit actor/snapshots/rollback: PASS')

        async with factory() as session:
            session.add_all([UserRole(user_id=1, role='MANAGER'), Hotel(id=1, name='Hotel'),
                             Package(id=1, name='Standard', price_per_photo=400)])
            await session.commit()
        async def rows(conn, sql, **params):
            return list((await conn.execute(text(sql), params)).mappings())
        api = SimpleNamespace(engine=engine, rows=rows, day=date.fromisoformat,
                              today=lambda: date(2026, 9, 22), tz=ZoneInfo('Europe/Moscow'))
        body = {'actorId': 1, 'kind': 'booking', 'key': 'postgres-concurrent-booking', 'date': '2026-09-22',
                'data': {'client_name': 'Guest', 'client_phone': None, 'hotel_id': 1, 'package_id': 1,
                         'room': '100', 'guest_count': 1, 'deposit': '0', 'shoot_date': '2026-09-22',
                         'shoot_time': '12:00', 'photographer_id': None}}
        work = Workday(api)
        results = await asyncio.gather(*(work.execute({'id': 1, 'roles': ['MANAGER']}, body) for _ in range(2)))
        assert results[0].text == results[1].text
        async with factory() as session:
            assert len((await session.scalars(select(Booking))).all()) == 1
            assert len((await session.scalars(select(OperationRequest))).all()) == 1
        print('PostgreSQL concurrent replay: one booking and one operation acknowledgement: PASS')
        async def audit(conn,actor,action,entity,eid,details):
            await conn.execute(text('INSERT INTO audit_logs(user_id,action,entity,entity_id,details,created_at) VALUES (:uid,:action,:entity,:eid,:details,CURRENT_TIMESTAMP)'),
                {'uid':actor['id'],'action':action,'entity':entity,'eid':eid,'details':details})
        api.audit_write=audit
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE user_roles SET role='OWNER' WHERE user_id=1"))
            await conn.execute(text("INSERT INTO settings(key,value) VALUES ('MANAGER_PERCENT','5'),('private-secret','redacted')"))
            await conn.execute(text("UPDATE settings SET value='7' WHERE key='MANAGER_PERCENT'"))
            captured=await rows(conn,"SELECT details FROM audit_logs WHERE entity='settings' ORDER BY id")
            assert len(captured)==2 and 'private-secret' not in str(captured)
            assert json.loads(captured[-1]['details'])['before']['value']=='5'
        cashbody={'actorId':1,'key':'postgres-concurrent-expense','kind':'cash_expense','date':'2026-09-22',
            'data':{'category':'OTHER','amount':'123.45','paidOn':'2026-09-22','hotelId':None,'employeeId':None,'note':'Paid supplies','paidConfirmed':True}}
        results=await asyncio.gather(*(work.execute({'id':1,'roles':['OWNER']},cashbody) for _ in range(2)))
        assert results[0].text==results[1].text
        async with factory() as session:
            assert len((await session.scalars(select(CashMovement))).all())==1
        print('PostgreSQL concurrent expense replay and financial settings audit: PASS')
        from app.development import queue_reviews
        from app.models import utc_now
        async with factory() as session:
            booking=await session.get(Booking,1);booking.photographer_id=1
            shooting=await session.scalar(select(Shooting).where(Shooting.booking_id==1))
            shooting.full_upload_completed_at=utc_now()
            session.add(Photo(shooting_id=shooting.id,file_id='fixture-frame'))
            session.add(Sale(booking_id=1,created_by_id=1,credited_user_id=1,sold_photos=1,amount=400,percent=0,commission=0))
            await session.commit()
        async def enqueue():
            async with factory() as session: return await queue_reviews(session)
        await asyncio.gather(enqueue(),enqueue())
        async with factory() as session:
            reviews=(await session.scalars(select(ShootDevelopmentReview))).all()
            assert len(reviews)==1 and reviews[0].summary_parts=='[]'
        print('PostgreSQL concurrent AI enqueue: one review per complete frame set: PASS')

        from app.miniapp_security import AccessError
        from app.people import People
        async with factory() as session:
            session.add_all([User(id=2,tg_id=9876543211,name='Admin'), UserRole(user_id=2,role='ADMIN')])
            await session.commit()
        async with engine.begin() as conn:
            # The legacy fixture above seeds an explicit id; align only its sequence.
            await conn.execute(text("SELECT setval(pg_get_serial_sequence('hotels','id'), (SELECT max(id) FROM hotels))"))
        async def hotel_body(request):
            return request['body']
        api.body=hotel_body
        api.bot=SimpleNamespace()
        people=People(api)
        requests=[{'miniapp_actor':{'id':uid,'roles':[role]},'body':{'name':'Concurrent Hotel'}}
                  for uid,role in [(1,'OWNER'),(2,'ADMIN')]]
        async with asyncio.timeout(15):
            results=await asyncio.gather(*(people.create_hotel(request) for request in requests),return_exceptions=True)
        assert sum(not isinstance(r,Exception) and r.status==201 for r in results)==1, results
        assert sum(isinstance(r,AccessError) and r.status==409 for r in results)==1, results
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM hotels WHERE name='Concurrent Hotel'"))==1
        print('PostgreSQL concurrent hotel additions: one hotel, duplicate rejected, audit retained: PASS')

        # Queue claims must remain unique across simultaneous PostgreSQL workers.
        from unittest.mock import patch

        from app.models import PhotoStorage
        from app.services import photo_storage
        async with factory() as session:
            photo = await session.scalar(select(Photo))
            session.add(PhotoStorage(photo_id=photo.id, shooting_id=photo.shooting_id,
                                     telegram_file_id='fixture-original', source_kind='DOCUMENT'))
            await session.commit()
        with patch.object(photo_storage, 'Session', factory):
            claims = await asyncio.gather(photo_storage._claim_job(), photo_storage._claim_job())
        assert sum(claim is not None for claim in claims) == 1, claims
        async with factory() as session:
            stored = await session.scalar(select(PhotoStorage))
            assert stored.status == 'UPLOADING' and stored.attempts == 1
        print('PostgreSQL concurrent storage claims: exactly one worker owns the original: PASS')



    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
