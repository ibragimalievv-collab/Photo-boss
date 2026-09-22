import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Hotel, PayrollEntry, Sale, SaleDraftPhoto, User
from app.services.bookings import create_booking_record
from app.services.commissions import (
    hotel_photo_price,
    manager_booking_bonus,
    photographer_bonus,
    photographer_percent,
)
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)
from tests.test_workflow_services import booking_data, fixture


@pytest.mark.parametrize('count,rate', [(149,10),(150,15)])
def test_frame_boundary(count, rate):
    assert photographer_percent(count) == rate


@pytest.mark.parametrize('amount,bonus', [('20999',0),('21000',1000),('25999',1000),('26000',1500),('30999.99',1500),('31000',2000)])
def test_sale_bonus_boundary(amount, bonus):
    assert photographer_bonus(amount) == bonus


@pytest.mark.parametrize('count,bonus', [(4,0),(5,500),(6,600)])
def test_booking_bonus_boundary(count, bonus):
    assert manager_booking_bonus(count) == bonus


@pytest.mark.parametrize('name,price', [('Гранд',500),(' Эллада ',300),('Бетон',400)])
def test_hotel_price(name, price):
    assert hotel_photo_price(name) == price


def test_discount_sale_bonus_and_retry():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as s:
                manager, photo = await s.get(User,1), await s.get(User,2)
                (await s.get(Hotel,1)).name = 'Гранд'
                b = await create_booking_record(s,manager,booking_data())
                b.photographer_id=2
                b.status='READY_FOR_SALE'
                await s.flush()
                d = await start_sale_draft(s,photo,{'PHOTOGRAPHER'},b.id)
                await capture_draft_receipt(s,photo,d,'r','u','h',{})
                with pytest.raises(ValueError,match='всю съёмку'):
                    await set_sale_counts(s,photo,d,150,100,50)
                for bad in ('NaN','Infinity',51,-1,'0.001'):
                    with pytest.raises(ValueError):
                        await set_sale_counts(s,photo,d,100,100,bad)
                assert await set_sale_counts(s,photo,d,104,104,50) == 26000
                # Persisted terms survive a hotel rename while photos upload.
                (await s.get(Hotel,1)).name='Эллада'
                for n in range(104):
                    s.add(SaleDraftPhoto(draft_id=d.id,telegram_file_id=str(n),telegram_unique_id=str(n),storage_path=str(n),sha256=str(n),byte_size=100))
                await s.flush()
                result=await complete_sale(s,photo,d.id)
                assert result.sale.amount == 26000
                assert result.sale.unit_price == 500
                assert result.sale.discount_percent == 50
                entries=(await s.scalars(select(PayrollEntry))).all()
                assert {(r.user_id,r.kind,r.amount) for r in entries} == {(1,'Комиссия менеджера',3900),(2,'Бонус фотографа',1500)}
                assert all(f'sale={result.sale.id};' in r.note for r in entries)
                with pytest.raises(ValueError,match='завершена'):
                    await complete_sale(s,photo,d.id)
                assert await s.scalar(select(func.count(Sale.id))) == 1
                assert await s.scalar(select(func.count(PayrollEntry.id))) == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_manager_cannot_discount_and_daily_increment():
    async def run():
        engine, factory=await fixture()
        try:
            async with factory() as s:
                manager=await s.get(User,1)
                for n in range(6):
                    b=await create_booking_record(s,manager,booking_data())
                    entries=(await s.scalars(select(PayrollEntry))).all()
                    assert sum(Decimal(str(e.amount)) for e in entries) == manager_booking_bonus(n+1)
                b.photographer_id=2
                b.status='READY_FOR_SALE'
                await s.flush()
                d=await start_sale_draft(s,manager,{'MANAGER'},b.id)
                await capture_draft_receipt(s,manager,d,'r','u','h',{})
                with pytest.raises(ValueError,match='фотограф'):
                    await set_sale_counts(s,manager,d,100,100,50)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_additive_financial_migration_is_repeatable():
    async def run():
        from sqlalchemy import inspect, text

        from app.schema_updates import upgrade
        engine, _factory = await fixture()
        try:
            async with engine.begin() as conn:
                for table in ('sales', 'sale_drafts'):
                    for column in ('unit_price', 'discount_percent'):
                        await conn.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
                await upgrade(conn)
                await upgrade(conn)
                for table in ('sales', 'sale_drafts'):
                    columns = await conn.run_sync(lambda c, table=table: {r['name'] for r in inspect(c).get_columns(table)})
                    assert {'unit_price', 'discount_percent'} <= columns
                assert await conn.scalar(text('SELECT count(*) FROM users')) == 2
        finally:
            await engine.dispose()
    asyncio.run(run())
