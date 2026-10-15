"""Photographer incentives use the full shoot, once across split sales."""

import asyncio
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import (
    Booking,
    Client,
    Hotel,
    Package,
    PayrollEntry,
    Photo,
    Sale,
    SaleDraft,
    SaleDraftPhoto,
    Setting,
    Shooting,
    User,
    UserRole,
)
from app.services.commissions import (
    PHOTO_BONUS_KIND,
    photographer_bonus,
    reconcile_photographer_bonus,
)
from app.services.sale_workflow import complete_sale, finalize_photographer_commissions


@pytest.mark.parametrize(("revenue", "bonus"), [
    ("0", "0.00"), ("20999.99", "0.00"), ("21000", "1000.00"),
    ("25999.99", "1000.00"), ("26000", "1500.00"),
    ("30999.99", "1500.00"), ("31000", "2000.00"),
    ("41000", "3000.00"),
])
def test_bonus_at_complete_revenue_thresholds(revenue, bonus):
    assert photographer_bonus(revenue) == Decimal(bonus)


@pytest.mark.parametrize("revenue", ["NaN", "Infinity", "invalid", None])
def test_bonus_rejects_invalid_revenue(revenue):
    with pytest.raises(ValueError, match="сумма"):
        photographer_bonus(revenue)


async def fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([
            User(id=1, tg_id=1, name="Manager"),
            User(id=2, tg_id=2, name="Photographer"),
            UserRole(user_id=1, role="MANAGER"),
            UserRole(user_id=2, role="PHOTOGRAPHER"),
            Hotel(id=1, name="Hotel"),
            Client(id=1, name="Guest", phone="+79991234567"),
            Package(id=1, name="Standard", price_per_photo=400),
            Setting(key="MANAGER_PERCENT", value="15"),
        ])
        await session.flush()
        session.add(Booking(
            id=1, hotel_id=1, client_id=1, room="100", shoot_date=date(2026, 10, 15),
            shoot_time=time(12), package_id=1, manager_id=1, photographer_id=2,
            status="READY_FOR_SALE",
        ))
        await session.flush()
        session.add(Shooting(id=1, booking_id=1, status="READY_FOR_SALE"))
        await session.commit()
    return engine, factory


async def draft(session, sold):
    result = SaleDraft(
        booking_id=1, created_by_id=1, status="AWAITING_SELECTED",
        declared_photo_count=150, sold_photos=sold,
    )
    session.add(result)
    await session.flush()
    session.add_all(SaleDraftPhoto(
        draft_id=result.id, telegram_file_id=f"{result.id}:{index}",
        telegram_unique_id=f"{result.id}:{index}",
        storage_path=f"app:/selected/{result.id}/{index}.jpg",
        sha256=f"{result.id}:{index}", byte_size=100,
    ) for index in range(sold))
    await session.flush()
    return result


def test_sale_awards_one_bonus_across_split_sales_and_keeps_both_commissions():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                manager = await session.get(User, 1)
                first = await complete_sale(session, manager, (await draft(session, 53)).id)
                bonus = await session.scalar(select(PayrollEntry).where(
                    PayrollEntry.kind == PHOTO_BONUS_KIND))
                assert first.sale.amount == 21200
                assert bonus.user_id == 2 and bonus.amount == 1000
                bonus_id = bonus.id
                second_draft = await draft(session, 12)
                second = await complete_sale(session, manager, second_draft.id)
                assert second.sale.amount == 4800
                assert bonus.id == bonus_id and bonus.amount == 1500
                with pytest.raises(ValueError, match="завершена"):
                    await complete_sale(session, manager, second_draft.id)
                assert await session.scalar(select(func.count(PayrollEntry.id)).where(
                    PayrollEntry.kind == PHOTO_BONUS_KIND)) == 1
                assert await session.scalar(select(func.sum(PayrollEntry.amount)).where(
                    PayrollEntry.user_id == 1)) == 3900

                # Only the full made-photo count finalizes the 15% tier; buying
                # just 65 photos must not downgrade it to 10%.
                session.add_all(Photo(shooting_id=1, file_id=str(index))
                                for index in range(150))
                shooting = await session.get(Shooting, 1)
                shooting.full_upload_completed_at = datetime(2026, 10, 15, 12, tzinfo=UTC).replace(tzinfo=None)
                booking = await session.get(Booking, 1)
                result = await finalize_photographer_commissions(session, booking)
                assert result["percent"] == 15
                assert first.sale.commission + second.sale.commission == 3900
                assert bonus.amount == 1500
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_bonus_reconciliation_handles_retry_revenue_correction_and_sale_removal():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                booking = await session.get(Booking, 1)
                sales = [Sale(
                    booking_id=1, created_by_id=1, credited_user_id=2,
                    commission_role="PHOTOGRAPHER", sold_photos=1,
                    amount=amount, percent=10, commission=amount / 10,
                ) for amount in (20000, 6000)]
                session.add_all(sales)
                await reconcile_photographer_bonus(session, booking)
                await reconcile_photographer_bonus(session, booking)
                bonus = await session.scalar(select(PayrollEntry).where(
                    PayrollEntry.kind == PHOTO_BONUS_KIND))
                assert bonus.amount == 1500
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                bonus_id = bonus.id

                sales[0].amount = 10000
                await reconcile_photographer_bonus(session, booking)
                assert bonus.id == bonus_id and bonus.amount == 0
                sales[0].amount = 21000
                await reconcile_photographer_bonus(session, booking)
                assert bonus.id == bonus_id and bonus.amount == 1500
                for sale in sales:
                    await session.delete(sale)
                await reconcile_photographer_bonus(session, booking)
                assert bonus.id == bonus_id and bonus.amount == 0
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
