import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import (
    Booking,
    Hotel,
    Package,
    PayrollEntry,
    Receipt,
    Sale,
    SaleDraftPhoto,
    User,
    UserRole,
)
from app.services.bookings import create_booking_record
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)


async def fixture(url='sqlite+aiosqlite:///:memory:'):
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([User(id=1, tg_id=1, name='Manager'), User(id=2, tg_id=2, name='Photo'),
                         UserRole(user_id=1, role='MANAGER'), UserRole(user_id=2, role='PHOTOGRAPHER'),
                         Hotel(id=1, name='Hotel'), Package(id=1, name='Standard', price_per_photo=400)])
        await session.commit()
    return engine, factory


def booking_data():
    return {'client_name': 'Guest', 'client_phone': None, 'hotel_id': 1, 'package_id': 1,
            'room': '100', 'guest_count': 2, 'deposit': '0', 'shoot_date': '2026-09-22', 'shoot_time': '12:00'}


def test_shared_sale_uses_server_price_receipt_and_single_commission():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                manager = await session.get(User, 1)
                booking = await create_booking_record(session, manager, booking_data())
                booking.photographer_id = 2
                booking.status = 'READY_FOR_SALE'
                await session.flush()
                draft = await start_sale_draft(session, manager, {'MANAGER'}, booking.id)
                await capture_draft_receipt(session, manager, draft, 'receipt', 'unique', 'hash', {'status': 'not_configured'})
                assert await set_sale_counts(session, manager, draft, 150, 2) == 800
                with pytest.raises(ValueError, match='выбранные'):
                    await complete_sale(session, manager, draft.id)
                for n in range(2):
                    session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id=str(n), telegram_unique_id=str(n),
                                             storage_path=f'app:/selected/{n}.jpg', sha256=str(n), byte_size=100))
                await session.flush()
                result = await complete_sale(session, manager, draft.id)
                assert result.sale.amount == 800 and result.sale.percent == 0
                assert result.sale.payment_status == 'UNPAID'
                assert result.receipt.status == 'PENDING'
                await session.commit()
                with pytest.raises(ValueError, match='завершена'):
                    await complete_sale(session, manager, draft.id)
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                assert await session.scalar(select(func.count(Receipt.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_booking_rechecks_roles_and_active_references():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                photo = await session.get(User, 2)
                manager = await session.get(User, 1)
                with pytest.raises(ValueError, match='Нет доступа'):
                    await create_booking_record(session, photo, booking_data())
                with pytest.raises(ValueError, match='Назначать фотографа'):
                    await create_booking_record(session, manager, booking_data(), 2)
                bad = booking_data() | {'deposit': 'NaN'}
                with pytest.raises(ValueError, match='Проверьте'):
                    await create_booking_record(session, manager, bad)
                (await session.get(Hotel, 1)).active = False
                await session.flush()
                with pytest.raises(ValueError, match='больше не доступны'):
                    await create_booking_record(session, manager, booking_data())
                assert await session.scalar(select(func.count(Booking.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())
