"""Explicit paid expenses and payouts; never manufacture a payroll accrual."""
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from aiohttp import web
from sqlalchemy import insert, text

from .miniapp_security import AccessError, require_owner
from .models import CashMovement, utc_now
from .people import People, positive_id
from .services.insights import cents

CATEGORIES = {'HOTEL':'Расходы отеля','TAX':'Налоги','PAYROLL':'Выплата сотруднику','OTHER':'Другой оплаченный расход'}


class CashControl:
    def __init__(self,api):
        self.api=api

    async def record(self,conn,actor,data):
        require_owner(actor['roles'])
        if set(data)!={'category','amount','paidOn','hotelId','employeeId','note','paidConfirmed'} or data['paidConfirmed'] is not True:
            raise AccessError('Подтвердите фактическую оплату расхода.',400)
        if not isinstance(data['category'],str) or data['category'] not in CATEGORIES or not isinstance(data['note'],str) or not 3<=len(data['note'].strip())<=1000:
            raise AccessError('Укажите категорию и основание расхода.',400)
        try:
            if not isinstance(data['amount'],str):
                raise TypeError
            amount=Decimal(data['amount'])
            if not amount.is_finite() or not 0<amount<=Decimal('999999999.99') or amount!=amount.quantize(Decimal('.01')):
                raise ValueError
        except (ValueError,TypeError,InvalidOperation) as exc:
            raise AccessError('Сумма — положительное число с точностью до копейки.',400) from exc
        day=self.api.day(data['paidOn'])
        if not self.api.today()-timedelta(days=366)<=day<=self.api.today():
            raise AccessError('Укажите фактическую дату оплаты за последние 367 дней.',400)
        hotel=positive_id(data['hotelId']) if data['hotelId'] is not None else None
        employee=positive_id(data['employeeId']) if data['employeeId'] is not None else None
        if (data['category']=='PAYROLL') != (employee is not None):
            raise AccessError('Сотрудник указывается только для фактической выплаты зарплаты.',400)
        if hotel is not None and not await self.api.rows(conn,'SELECT id FROM hotels WHERE id=:id',id=hotel):
            raise AccessError('Отель не найден.',404)
        if employee is not None and not await self.api.rows(conn,'SELECT id FROM users WHERE id=:id',id=employee):
            raise AccessError('Сотрудник не найден.',404)
        eid=await conn.scalar(insert(CashMovement).values(category=data['category'],amount=amount,paid_on=day,
            hotel_id=hotel,employee_id=employee,note=data['note'].strip(),status='POSTED',created_by_id=actor['id'],created_at=utc_now()).returning(CashMovement.id))
        await self.api.audit_write(conn,actor,'paid_expense_recorded','cash_movement',eid,
            json.dumps({'before':None,'after':data},ensure_ascii=False))
        return {'cashMovementId':eid,'amount':cents(amount)}

    async def void(self,request):
        actor=request['miniapp_actor'];require_owner(actor['roles'])
        eid=positive_id(request.match_info['id']);body=await self.api.body(request)
        if set(body)!={'reason'} or not isinstance(body['reason'],str) or not 3<=len(body['reason'].strip())<=1000:
            raise AccessError('Укажите основание отмены записи.',400)
        async with self.api.engine.begin() as conn:
            require_owner(await People(self.api).current_editor(conn,actor['id']))
            rows=await self.api.rows(conn,'SELECT id,status FROM cash_movements WHERE id=:id FOR UPDATE',id=eid)
            if not rows:
                raise AccessError('Расход не найден.',404)
            if rows[0]['status']=='VOIDED':
                return web.json_response({'ok':True,'alreadyVoided':True})
            await conn.execute(text("UPDATE cash_movements SET status='VOIDED',voided_by_id=:uid,voided_at=:now,void_reason=:reason WHERE id=:id"),
                {'id':eid,'uid':actor['id'],'now':utc_now(),'reason':body['reason'].strip()})
            await self.api.audit_write(conn,actor,'paid_expense_voided','cash_movement',eid,
                json.dumps({'before':'POSTED','after':'VOIDED','reason':body['reason'].strip()},ensure_ascii=False))
        return web.json_response({'ok':True})


def install_cash_control(app,api):
    app.router.add_post('/api/miniapp/cash-movements/{id}/void',CashControl(api).void)
