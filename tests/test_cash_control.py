import asyncio
import json
from datetime import date

import pytest
from sqlalchemy import select, text
from test_team import OWNER, Request
from test_workday import fixture

from app.audit_capture import install_capture
from app.cash_control import CashControl
from app.miniapp_security import AccessError
from app.models import AuditLog, CashMovement, OperationRequest, PayrollEntry, Receipt
from app.services.insights import anomalies, period_data


def operation(**data):
    return {'actorId':1,'key':'cash-expense-idempotency-1','kind':'cash_expense','date':'2026-09-21',
            'data':{'category':'PAYROLL','amount':'100.25','paidOn':'2026-09-20','hotelId':1,'employeeId':2,'note':'Salary paid','paidConfirmed':True}|data}


def test_paid_expense_once_void_restores_flow_without_changing_accruals():
    async def run():
        engine,factory,work=await fixture()
        async with factory() as s:
            r=await s.get(Receipt,1);r.status='APPROVED';r.verified_amount=800;r.reviewed_at=r.created_at
            await s.commit()
        body=operation()
        for _ in range(2): await work.operation(Request(OWNER,body))
        async with engine.connect() as conn:
            report=await period_data(work.api,conn,date(2026,9,20),date(2026,9,20))
            assert report['metrics']['paidOut']==10025 and report['metrics']['netCash']==69975
            assert report['metrics']['cashAfterAccruals']==68000
        async with factory() as s:
            assert len((await s.scalars(select(CashMovement))).all())==1
            assert len((await s.scalars(select(OperationRequest))).all())==1
            assert not (await s.scalars(select(PayrollEntry))).all()
        cash=CashControl(work.api)
        for _ in range(2): await cash.void(Request(OWNER,{'reason':'Duplicate manual entry'},1))
        async with engine.connect() as conn:
            report=await period_data(work.api,conn,date(2026,9,20),date(2026,9,20))
            assert report['metrics']['netCash']==80000 and report['paidMovements'][0]['status']=='VOIDED'
        with pytest.raises(AccessError) as error:
            await work.operation(Request(OWNER,operation(amount='100.26')))
        assert error.value.status==409
        await engine.dispose()
    asyncio.run(run())


def test_cash_permissions_amounts_dates_and_confirmation_checked_on_server():
    async def run():
        engine,_factory,work=await fixture()
        cash=CashControl(work.api)
        for patch in [{'amount':'NaN'},{'amount':'Infinity'},{'amount':'-1'},{'amount':'1.001'},{'amount':10},
                      {'paidConfirmed':False},{'paidOn':'2026-09-22'},{'employeeId':None},{'category':'TAX'},{'hotelId':999}]:
            with pytest.raises(AccessError): await work.operation(Request(OWNER,operation(**patch)))
        with pytest.raises(AccessError):
            async with engine.begin() as conn: await cash.record(conn,{'id':1,'roles':['ADMIN']},operation()['data'])
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE user_roles SET role='ADMIN' WHERE user_id=1"))
        with pytest.raises(AccessError): await work.operation(Request(OWNER,operation()))
        with pytest.raises(AccessError): await cash.void(Request(OWNER,{'reason':'Wrong role'},1))
        await engine.dispose()
    asyncio.run(run())


def test_financial_setting_snapshots_exclude_unrelated_secrets():
    async def run():
        engine,factory,_work=await fixture()
        # Register the same audit function on this already-open fixture connection.
        async with engine.begin() as conn:
            driver=(await conn.get_raw_connection()).driver_connection
            await driver.create_function('pb_actor_id',0,lambda:1)
            await install_capture(conn)
            await conn.execute(text("INSERT INTO settings(key,value) VALUES ('MANAGER_PERCENT','5'),('private-secret','do-not-record')"))
            await conn.execute(text("UPDATE settings SET value='7' WHERE key='MANAGER_PERCENT'"))
            await conn.execute(text("UPDATE settings SET value='still-private' WHERE key='private-secret'"))
        async with factory() as s:
            logs=(await s.scalars(select(AuditLog).where(AuditLog.entity=='settings'))).all()
            assert len(logs)==2 and all('private' not in log.details for log in logs)
            update=json.loads(logs[-1].details)
            assert update['before']['value']=='5' and update['after']['value']=='7'
        await engine.dispose()
    asyncio.run(run())


def test_anomalous_price_requires_ten_comparable_previous_sales():
    async def run():
        engine,factory,work=await fixture()
        from app.models import Sale
        async with factory() as s:
            base=await s.get(Sale,1)
            for n in range(2,12):
                s.add(Sale(id=n,booking_id=1,created_by_id=2,credited_user_id=2,sold_photos=2,amount=800 if n<11 else 4000,percent=0,commission=0,created_at=base.created_at))
            await s.commit()
        async with engine.connect() as conn:
            flags=[f for f in await anomalies(work.api,conn) if f['key'].endswith('unusual-price')]
            assert len(flags)==1 and flags[0]['entityId']==11 and flags[0]['evidence']['sampleSize']==10
        await engine.dispose()
    asyncio.run(run())
