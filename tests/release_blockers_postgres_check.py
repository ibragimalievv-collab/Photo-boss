"""Real PostgreSQL race and hotel-scope checks in a disposable localhost schema."""
import asyncio
import os
import uuid
from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_release_blockers import confirm_photos

from app.db import Base
from app.miniapp_api import MiniApp
from app.models import (
    Booking,
    Client,
    Hotel,
    HotelEmployee,
    Package,
    PayrollEntry,
    Photo,
    Receipt,
    Sale,
    SaleDraftPhoto,
    Shooting,
    User,
    UserRole,
)
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_full_upload,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)


async def main():
    url = os.environ['BLOCKER_POSTGRES_TEST_URL']
    assert make_url(url).host in {'localhost', '127.0.0.1'}, 'Only a local disposable test database is allowed'
    schema = 'pb_blockers_' + uuid.uuid4().hex
    admin_engine = create_async_engine(url)
    async with admin_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        api = MiniApp(engine, SimpleNamespace(), [])
        async with factory() as session:
            session.add_all([User(id=1, tg_id=1, name='Manager'), User(id=2, tg_id=2, name='Photographer'),
                User(id=3, tg_id=3, name='Admin'), UserRole(user_id=1, role='MANAGER'),
                UserRole(user_id=2, role='PHOTOGRAPHER'), UserRole(user_id=3, role='ADMIN'),
                Hotel(id=1, name='Allowed'), Hotel(id=2, name='Not allowed'),
                Package(id=1, name='Test', price_per_photo=400), Client(id=1, name='Test guest')])
            await session.flush()
            session.add(HotelEmployee(user_id=3, hotel_id=1))
            session.add(Booking(id=1, client_id=1, hotel_id=1, package_id=1, manager_id=1,
                photographer_id=2, room='TEST', guest_count=1, deposit=0,
                shoot_date=api.today(), shoot_time=__import__('datetime').time(12), status='READY_FOR_SALE'))
            await session.flush()
            shooting = Shooting(booking_id=1, status='READY_FOR_SALE')
            session.add(shooting)
            await session.flush()
            sid = shooting.id
            session.add_all(Photo(shooting_id=sid, file_id=f'test-{n}') for n in range(150))
            await session.flush()
            await confirm_photos(session, shooting)
            actor = await session.get(User, 1)
            draft = await start_sale_draft(session, actor, {'MANAGER'}, 1)
            await capture_draft_receipt(session, actor, draft, 'r', 'r', 'r', {'status': 'not_configured'})
            await set_sale_counts(session, actor, draft, 150, 1)
            session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id='selected', telegram_unique_id='selected',
                storage_path='app:/test/selected.jpg', sha256='1' * 64, byte_size=100))
            await session.commit()
            draft_id = draft.id
        saved_uncommitted, allow_commit = asyncio.Event(), asyncio.Event()

        async def sale_task():
            async with factory() as session:
                result = await complete_sale(session, await session.get(User, 1), draft_id)
                assert result.sale.percent == 0
                saved_uncommitted.set()
                await allow_commit.wait()
                await session.commit()

        async def close_task():
            async with factory() as session:
                await complete_full_upload(session, await session.get(User, 2), sid)
                await session.commit()

        sale = asyncio.create_task(sale_task())
        await asyncio.wait_for(saved_uncommitted.wait(), 10)
        close = asyncio.create_task(close_task())
        try:
            try:
                await asyncio.wait_for(asyncio.shield(close), .2)
                raise AssertionError('Closure bypassed the uncommitted sale booking lock')
            except TimeoutError:
                pass
        finally:
            allow_commit.set()
        await asyncio.wait_for(asyncio.gather(sale, close), 10)
        async with factory() as session:
            row = await session.scalar(select(Sale))
            assert row.percent == 15 and row.commission == 60 and row.commission_finalized_at
            assert (await session.get(Shooting, sid)).full_upload_completed_at
            session.add(Booking(id=2, client_id=1, hotel_id=2, package_id=1, manager_id=1,
                photographer_id=2, room='OTHER', guest_count=1, deposit=0,
                shoot_date=api.today(), shoot_time=__import__('datetime').time(13), status='READY_FOR_SALE'))
            await session.flush()
            wrong_entry = PayrollEntry(user_id=1, kind='Комиссия менеджера', amount=1500, period='test')
            session.add(wrong_entry)
            await session.flush()
            session.add(Sale(booking_id=2, created_by_id=1, credited_user_id=2, sold_photos=25,
                amount=10000, percent=15, commission=1500, manager_payroll_entry_id=wrong_entry.id))
            session.add(Receipt(booking_id=2, uploaded_by_id=1, purpose='PAYMENT', file_id='other',
                file_unique_id='other', expected_amount=10000, verified_amount=10000,
                status='APPROVED', reviewed_at=__import__('datetime').datetime.utcnow()))
            await session.commit()
        async with engine.connect() as conn:
            report = await api.finance_data(conn, {'id': 3, 'roles': ['ADMIN'], 'permissions': {'financeScope': 'today'}},
                                            api.today(), api.today(), 'today')
            assert report['sales'] == 40000 and report['cashReceived'] == 0
            assert [s['bookingId'] for s in report['salesRows']] == [1]
            assert report['payroll'] < 100000
        print('PASS: real PostgreSQL serializes sale/closure; no lost commission; admin cash and payroll remain hotel-scoped')
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
