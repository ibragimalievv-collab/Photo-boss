"""The agreed personal manager rate takes priority over global defaults."""
import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from test_workflow_services import booking_data, fixture

from app.models import Compensation, PayrollEntry, Sale, SaleDraftPhoto, Setting, User
from app.services.bookings import create_booking_record
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)


async def ready_sale(session):
    manager = await session.get(User, 1)
    booking = await create_booking_record(session, manager, booking_data())
    booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
    draft = await start_sale_draft(session, manager, {'MANAGER'}, booking.id)
    await capture_draft_receipt(session, manager, draft, 'receipt', 'receipt', 'receipt', {})
    await set_sale_counts(session, manager, draft, 150, 2)
    session.add_all(SaleDraftPhoto(draft_id=draft.id, telegram_file_id=str(n),
        telegram_unique_id=str(n), storage_path=f'app:/selected/{n}.jpg',
        sha256=str(n), byte_size=100) for n in range(2))
    await session.flush()
    return manager, draft


@pytest.mark.parametrize('personal_rate', [Decimal('17.5'), Decimal(0), Decimal(100)])
def test_personal_manager_rate_overrides_global_setting_and_is_frozen(personal_rate):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                session.add(Setting(key='MANAGER_PERCENT', value='15'))
                personal = Compensation(user_id=1, role='MANAGER', sales_percent=float(personal_rate))
                session.add(personal)
                manager, draft = await ready_sale(session)
                result = await complete_sale(session, manager, draft.id)
                entry = await session.get(PayrollEntry, result.sale.manager_payroll_entry_id)
                expected = (Decimal(800) * personal_rate / 100).quantize(Decimal('.01'))
                assert Decimal(result.sale.manager_percent_applied) == personal_rate
                assert Decimal(str(entry.amount)) == expected
                personal.sales_percent = 25
                await session.commit()
                assert Decimal(result.sale.manager_percent_applied) == personal_rate
                assert Decimal(str(entry.amount)) == expected
                with pytest.raises(ValueError, match='завершена'):
                    await complete_sale(session, manager, draft.id)
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_other_employee_or_role_compensation_cannot_override_manager_global_rate():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                session.add_all([Setting(key='MANAGER_PERCENT', value='15'),
                    Compensation(user_id=1, role='PHOTOGRAPHER', sales_percent=99),
                    Compensation(user_id=2, role='MANAGER', sales_percent=90)])
                manager, draft = await ready_sale(session)
                result = await complete_sale(session, manager, draft.id)
                entry = await session.get(PayrollEntry, result.sale.manager_payroll_entry_id)
                assert Decimal(result.sale.manager_percent_applied) == 15
                assert Decimal(str(entry.amount)) == 120
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('personal_rate', [-1, 101])
def test_invalid_personal_rate_cannot_commit_a_sale(personal_rate):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                session.add(Compensation(user_id=1, role='MANAGER', sales_percent=personal_rate))
                manager, draft = await ready_sale(session)
                await session.commit()
                with pytest.raises(ValueError, match='ставку менеджера'):
                    await complete_sale(session, manager, draft.id)
                await session.rollback()
                assert await session.scalar(select(func.count(Sale.id))) == 0
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())
