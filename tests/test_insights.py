import asyncio
from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.insights import Insights, daily_control
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Client,
    Hotel,
    Notification,
    Package,
    Receipt,
    Sale,
    Shooting,
    User,
    UserRole,
)
from app.schema_updates import upgrade
from app.services.insights import change, compare_periods, receipt_check, sync_events
from app.services.receipts import validate_extraction


async def fixture():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await upgrade(conn)
        await upgrade(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add_all([User(id=1,tg_id=1,name='Owner'),User(id=2,tg_id=2,name='Photo'),UserRole(user_id=1,role='OWNER'),Hotel(id=1,name='Hotel'),Client(id=1,name='Guest'),Package(id=1,name='Basic',price_per_photo=400)])
        await s.flush()
        s.add(Booking(id=1,hotel_id=1,client_id=1,room='1',shoot_date=date(2026,9,20),shoot_time=time(12),package_id=1,manager_id=1,photographer_id=2))
        await s.flush()
        s.add_all([Shooting(id=1,booking_id=1,completed_at=datetime(2026,9,20,9,tzinfo=ZoneInfo("UTC")).replace(tzinfo=None),full_upload_completed_at=datetime(2026,9,20,10,tzinfo=ZoneInfo("UTC")).replace(tzinfo=None)),
                   Sale(id=1,booking_id=1,created_by_id=2,credited_user_id=2,sold_photos=2,amount=800,percent=15,commission=120,created_at=datetime(2026,9,20,12,tzinfo=ZoneInfo("UTC")).replace(tzinfo=None)),
                   Receipt(id=1,booking_id=1,uploaded_by_id=2,purpose='PAYMENT',file_id='1',file_unique_id='1',expected_amount=800,created_at=datetime(2026,9,20,12,tzinfo=ZoneInfo("UTC")).replace(tzinfo=None))])
        await s.commit()
    async def rows(conn, sql, **params):
        return list((await conn.execute(text(sql), params)).mappings())
    api = SimpleNamespace(engine=engine,rows=rows,tz=ZoneInfo('Europe/Moscow'),today=lambda:date(2026,9,21))
    return engine, factory, api


def test_compare_and_anomalies_do_not_mutate_money_and_deduplicate():
    async def run():
        engine, factory, api = await fixture()
        async with engine.begin() as conn:
            report = await compare_periods(api,conn,date(2026,9,20),date(2026,9,20))
            assert report['metrics']['revenue']==80000
            assert report['metrics']['average']==80000
            assert report['previous']['from']=='2026-09-19'
            assert report['changes']['revenue']['percent'] is None
            for _ in range(2):
                items=await sync_events(api,conn,1)
                assert any(i['key']=='sale:1:commission' for i in items)
                assert any(i['key']=='sale:1:payment' for i in items)
        async with factory() as s:
            assert len((await s.scalars(select(Notification))).all())==3
            sale=await s.get(Sale,1)
            assert sale.amount==800 and sale.percent==15 and sale.payment_status=='UNPAID'
            sale.payment_status='PAID'
            await s.commit()
        async with engine.begin() as conn:
            await sync_events(api,conn,1)
        async with factory() as s:
            n=await s.scalar(select(Notification).where(Notification.event_key=='sale:1:payment'))
            assert n.resolved_at is not None
        await engine.dispose()
    asyncio.run(run())


def test_daily_digest_idempotent():
    async def run():
        engine,factory,_api=await fixture()
        now=datetime(2026,9,21,7,tzinfo=ZoneInfo('UTC'))
        async with factory() as s:
            assert await daily_control(s,now,'Europe/Moscow')==1
            await s.commit()
            assert await daily_control(s,now,'Europe/Moscow')==0
            await s.commit()
        await engine.dispose()
    asyncio.run(run())


def test_roles_enforced_without_trusting_ui():
    async def run():
        service=Insights(None)
        for role in ['ADMIN','MANAGER','PHOTOGRAPHER']:
            with pytest.raises(AccessError):
                await service.report({'miniapp_actor':{'id':1,'roles':[role]}})
            with pytest.raises(AccessError):
                await service.scan({'miniapp_actor':{'id':1,'roles':[role]}})
    asyncio.run(run())


def test_old_receipt_shape_supported_and_no_authenticity_claim():
    old={'is_receipt':True,'amount':'800','currency':'RUB','date':'2026-09-20','bank':'Bank','recipient':'Merchant','operation_id':'abc','payment_status':'paid','concerns':[]}
    assert validate_extraction(old)['time'] is None
    import json
    check=receipt_check({'analysis':json.dumps({'status':'extracted','fields':old}),'status':'PENDING','created_at':datetime(2026,9,20,tzinfo=ZoneInfo("UTC")).replace(tzinfo=None),'expected_amount':800})
    assert check['level']=='image_extracted' and not check['findings']
    assert 'не подтверждает' in check['limitation']
    assert change(0,0)['percent'] is None
    assert change(50,100)['percent']==-50


def test_upgrade_preserves_legacy_notifications():
    async def run():
        engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        async with engine.begin() as conn:
            await conn.execute(text('CREATE TABLE notifications (id INTEGER PRIMARY KEY,user_id INTEGER,text TEXT,sent BOOLEAN,created_at TIMESTAMP)'))
            await conn.execute(text("INSERT INTO notifications(id,user_id,text,sent) VALUES (1,1,'legacy',FALSE)"))
            await upgrade(conn)
            await upgrade(conn)
            assert (await conn.execute(text('SELECT text,kind FROM notifications'))).one()==('legacy','legacy')
        await engine.dispose()
    asyncio.run(run())


def test_partial_week_compares_same_weekdays_and_does_not_raise_drop_flags(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services import insights
    async def run():
        current = {'metrics': {'revenue': 0, 'sales': 0, 'bookings': 0, 'attendance': 0, 'cancellations': 0, 'late': 0, 'receiptIssues': 0}}
        previous = {'metrics': dict(current['metrics'], revenue=10000, sales=10)}
        provider = AsyncMock(side_effect=[current, previous])
        monkeypatch.setattr(insights, 'period_data', provider)
        api = SimpleNamespace(today=lambda: date(2026, 9, 21))
        result = await insights.compare_periods(api, None, date(2026, 9, 21), date(2026, 9, 21), offset_days=7)
        assert provider.call_args_list[1].args[2:] == (date(2026, 9, 14), date(2026, 9, 14))
        assert result['changes']['revenue']['percent'] == -100
        assert result['flags'] == []
    asyncio.run(run())
