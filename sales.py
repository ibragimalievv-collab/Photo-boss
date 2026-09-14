from aiogram import Router,F
from aiogram.fsm.state import StatesGroup,State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select
from ..db import Session
from ..models import *
from ..services.core import get_user,roles_of,has,audit
from ..keyboards import reply
r=Router()
class S(StatesGroup): booking=State(); credited=State(); role=State(); photos=State()
@r.message(F.text=='🧾 Продажа')
async def begin(m,state:FSMContext):
 async with Session() as s:
  u=await get_user(s,m.from_user.id); rs=await roles_of(s,u)
  if not has(rs,'PHOTOGRAPHER') and not has(rs,'MANAGER'): return await m.answer('Нет доступа.')
  rows=(await s.execute(select(Booking).order_by(Booking.id.desc()).limit(20))).scalars().all(); await state.set_state(S.booking); await m.answer('Введите ID записи:\n'+', '.join(str(x.id) for x in rows))
@r.message(S.booking)
async def b(m,state):
 try: bid=int(m.text)
 except: return await m.answer('Нужен числовой ID.')
 async with Session() as s:
  b=await s.get(Booking,bid)
  if not b: return await m.answer('Запись не найдена.')
  await state.update_data(booking=bid); await state.set_state(S.credited); await m.answer('Введите Telegram ID сотрудника, кому засчитать продажу:')
@r.message(S.credited)
async def cr(m,state):
 try: uid=int(m.text)
 except: return await m.answer('Нужен Telegram ID.')
 async with Session() as s:
  u=await get_user(s,uid)
  if not u: return await m.answer('Сотрудник не найден.')
  await state.update_data(credited=u.id); await state.set_state(S.role); await m.answer('Роль для комиссии: MANAGER или PHOTOGRAPHER')
@r.message(S.role)
async def rr(m,state):
 role=m.text.strip().upper()
 if role not in ('MANAGER','PHOTOGRAPHER'): return await m.answer('Введите MANAGER или PHOTOGRAPHER')
 await state.update_data(role=role); await state.set_state(S.photos); await m.answer('Сколько фото продано?')
@r.message(S.photos)
async def save(m,state):
 try: count=int(m.text)
 except: return await m.answer('Нужно число.')
 data=await state.get_data()
 async with Session() as s:
  u=await get_user(s,m.from_user.id); b=await s.get(Booking,data['booking']); p=await s.get(Package,b.package_id); price=p.price_per_photo if p else 400; amount=count*price
  comp=(await s.execute(select(Compensation).where(Compensation.user_id==data['credited'],Compensation.role==data['role']))).scalar_one_or_none(); pct=comp.sales_percent if comp else (15 if data['role']=='MANAGER' else 10); sale=Sale(booking_id=b.id,created_by_id=u.id,credited_user_id=data['credited'],commission_role=data['role'],sold_photos=count,amount=amount,percent=pct,commission=amount*pct/100); s.add(sale); await s.commit(); await audit(s,u,'sale_created','sale',sale.id,f'credited={data["credited"]};role={data["role"]}'); await state.clear(); await m.answer(f'Продажа #{sale.id} сохранена: {amount:.2f} ₽; комиссия {sale.commission:.2f} ₽.')
