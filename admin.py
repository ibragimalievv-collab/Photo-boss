from aiogram import Router,F
from aiogram.fsm.state import StatesGroup,State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select,func
from ..db import Session
from ..models import *
from ..services.core import get_user,roles_of,has,audit,ROLES
r=Router()
class E(StatesGroup): tg=State(); name=State(); roles=State()
class H(StatesGroup): name=State()
class P(StatesGroup): name=State(); price=State()
async def guard(m,s):
 u=await get_user(s,m.from_user.id); rs=await roles_of(s,u); return u,has(rs,'ADMIN')
@r.message(F.text=='👥 Сотрудники')
async def employees(m):
 async with Session() as s:
  u,ok=await guard(m,s)
  if not ok:return
  rows=(await s.execute(select(User).order_by(User.id.desc()).limit(50))).scalars().all(); out=[]
  for x in rows:
   rs=await roles_of(s,x); out.append(f'#{x.id} {x.name} tg:{x.tg_id} [{", ".join(rs)}] {"✅" if x.active else "⛔"}')
  await m.answer('\n'.join(out) or 'Сотрудников нет.\n\nДобавить: /add_employee')
@r.message(F.text=='🏨 Отели')
async def hotels(m):
 async with Session() as s:
  if not (await guard(m,s))[1]:return
  rows=(await s.execute(select(Hotel))).scalars().all(); await m.answer('\n'.join(f'#{x.id} {x.name} — {x.address or ""}' for x in rows) or 'Нет отелей.'); await m.answer('Добавить: /add_hotel')
@r.message(F.text=='📦 Пакеты')
async def packages(m):
 async with Session() as s:
  if not (await guard(m,s))[1]:return
  rows=(await s.execute(select(Package))).scalars().all(); await m.answer('\n'.join(f'#{x.id} {x.name}: {x.price_per_photo:.2f} ₽/фото' for x in rows) or 'Нет пакетов.'); await m.answer('Добавить: /add_package')
@r.message(F.text=='📋 Все записи')
async def allbook(m):
 async with Session() as s:
  if not (await guard(m,s))[1]:return
  n=(await s.execute(select(func.count(Booking.id)))).scalar() or 0; await m.answer(f'📋 Всего записей: {n}')
@r.message(F.text=='📸 Все съёмки')
async def allshoot(m):
 async with Session() as s:
  if not (await guard(m,s))[1]:return
  rows=(await s.execute(select(Shooting).order_by(Shooting.id.desc()).limit(30))).scalars().all(); await m.answer('\n'.join(f'#{x.id}: {x.status}' for x in rows) or 'Нет съёмок.')
@r.message(F.text=='💰 Продажи')
async def allsales(m):
 async with Session() as s:
  if not (await guard(m,s))[1]:return
  total=(await s.execute(select(func.coalesce(func.sum(Sale.amount),0)))).scalar() or 0; await m.answer(f'💰 Продажи сети: {total:.2f} ₽')
@r.message(F.text=='💵 Зарплаты/выплаты')
async def payroll(m):
 async with Session() as s:
  if not (await guard(m,s))[1]:return
  rows=(await s.execute(select(PayrollEntry).order_by(PayrollEntry.created_at.desc()).limit(50))).scalars().all(); await m.answer('\n'.join(f'{x.period} #{x.user_id} {x.kind}: {x.amount:.2f} ₽' for x in rows) or 'Выплат нет.')
@r.message(F.text=='📊 Отчёты')
async def reports(m):
 async with Session() as s:
  if not (await guard(m,s))[1]:return
  sales=(await s.execute(select(func.coalesce(func.sum(Sale.amount),0)))).scalar() or 0; photos=(await s.execute(select(func.coalesce(func.sum(Sale.sold_photos),0)))).scalar() or 0; await m.answer(f'📊 Сеть\nПродажи: {sales:.2f} ₽\nПродано фото: {photos}')
@r.message(F.text=='⚙️ Настройки')
async def settings(m): await m.answer('Настройки хранятся в БД. Ключи: PHOTO_PRICE, MANAGER_PERCENT, PHOTOGRAPHER_PERCENT. Используйте /set key value.')
@r.message(F.text=='📜 Аудит')
async def auditlog(m):
 async with Session() as s:
  if not (await guard(m,s))[1]:return
  rows=(await s.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(50))).scalars().all(); await m.answer('\n'.join(f'{x.created_at:%d.%m %H:%M} #{x.user_id} {x.action} {x.entity or ""}#{x.entity_id or ""}' for x in rows) or 'Аудит пуст.')
@r.message(F.text=='/add_employee')
async def addemp(m,state): await state.set_state(E.tg); await m.answer('Telegram ID:')
@r.message(E.tg)
async def etg(m,state): await state.update_data(tg=int(m.text)); await state.set_state(E.name); await m.answer('Имя:')
@r.message(E.name)
async def ename(m,state): await state.update_data(name=m.text); await state.set_state(E.roles); await m.answer('Роли через запятую: ADMIN,MANAGER,PHOTOGRAPHER')
@r.message(E.roles)
async def eroles(m,state):
 async with Session() as s:
  d=await state.get_data(); u=(await s.execute(select(User).where(User.tg_id==d['tg']))).scalar_one_or_none();
  if not u:u=User(tg_id=d['tg'],name=d['name']);s.add(u);await s.flush()
  for role in {x.strip().upper() for x in m.text.split(',') if x.strip() in ROLES and x.strip()!='OWNER'}: s.add(UserRole(user_id=u.id,role=role))
  await s.commit(); await state.clear(); await m.answer('Сотрудник сохранён.')
@r.message(F.text=='/add_hotel')
async def addhotel(m,state): await state.set_state(H.name); await m.answer('Название отеля:')
@r.message(H.name)
async def hname(m,state):
 async with Session() as s: s.add(Hotel(name=m.text)); await s.commit(); await state.clear(); await m.answer('Отель добавлен.')
@r.message(F.text=='/add_package')
async def addpack(m,state): await state.set_state(P.name); await m.answer('Название пакета:')
@r.message(P.name)
async def pname(m,state): await state.update_data(name=m.text); await state.set_state(P.price); await m.answer('Цена за фото:')
@r.message(P.price)
async def pprice(m,state):
 async with Session() as s: d=await state.get_data(); s.add(Package(name=d['name'],price_per_photo=float(m.text))); await s.commit(); await state.clear(); await m.answer('Пакет добавлен.')
@r.message(F.text.startswith('/set '))
async def setv(m):
 async with Session() as s:
  if not (await guard(m,s))[1]:return
  _,key,val=m.text.split(maxsplit=2); z=await s.get(Setting,key)
  if z:z.value=val
  else:s.add(Setting(key=key,value=val))
  await s.commit(); await m.answer('Настройка сохранена.')
