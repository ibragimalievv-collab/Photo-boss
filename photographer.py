from datetime import datetime
from aiogram import Router,F
from aiogram.types import Message,CallbackQuery
from ..db import Session
from ..models import *
from ..services.core import get_user,roles_of,has,audit
from ..keyboards import inline
r=Router()
@r.message(F.text=='📸 Мои съёмки')
async def shoots(m):
 async with Session() as s:
  u=await get_user(s,m.from_user.id); rs=await roles_of(s,u)
  if not has(rs,'PHOTOGRAPHER'): return
  q=await s.execute(select(Booking,Shooting,Hotel,Client).join(Shooting,Shooting.booking_id==Booking.id).join(Hotel,Hotel.id==Booking.hotel_id).join(Client,Client.id==Booking.client_id).where(Booking.photographer_id==u.id).order_by(Booking.shoot_date,Booking.shoot_time))
  rows=q.all()
  if not rows: return await m.answer('Съёмок нет.')
  for b,sh,h,c in rows: await m.answer(f'📸 Съёмка #{b.id}\n🏨 {h.name}\n🚪 {b.room}\n👤 {c.name}\n📅 {b.shoot_date} {b.shoot_time}\nСтатус: {sh.status}',reply_markup=inline([[('▶️ Принять',f'accept:{sh.id}'),('📍 Прибыл',f'arrive:{sh.id}')],[('▶️ Начать',f'start:{sh.id}'),('✅ Готово',f'done:{sh.id}')]]))
@r.callback_query(F.data.startswith(('accept:','arrive:','start:','done:')))
async def action(c:CallbackQuery):
 act,sid=c.data.split(':'); sid=int(sid)
 async with Session() as s:
  u=await get_user(s,c.from_user.id); sh=await s.get(Shooting,sid)
  if not sh: return await c.answer('Нет')
  b=await s.get(Booking,sh.booking_id)
  if b.photographer_id!=u.id: return await c.answer('Это не ваша съёмка')
  now=datetime.utcnow(); sh.status={'accept':'ACCEPTED','arrive':'ARRIVED','start':'SHOOTING','done':'READY_FOR_MANAGER'}[act]
  if act=='accept': sh.accepted_at=now
  if act=='arrive': sh.arrived_at=now
  if act=='start': sh.started_at=now
  if act=='done': sh.completed_at=now; b.status='READY_FOR_MANAGER'
  await s.commit(); await audit(s,u,f'shooting_{act}','shooting',sid); await c.message.answer('Готово: '+sh.status); await c.answer()
@r.message(F.text=='📊 Моя статистика')
async def stats(m):
 async with Session() as s:
  u=await get_user(s,m.from_user.id); n=(await s.execute(select(func.count(Photo.id)).join(Shooting,Shooting.id==Photo.shooting_id).join(Booking,Booking.id==Shooting.booking_id).where(Booking.photographer_id==u.id))).scalar() or 0
  rev=(await s.execute(select(func.coalesce(func.sum(Sale.amount),0)).where(Sale.credited_user_id==u.id))).scalar() or 0
  await m.answer(f'📊 Статистика\nФото: {n}\nПродажи засчитаны: {rev:.2f} ₽')
@r.message(F.text=='💰 Мои выплаты')
async def payouts(m):
 async with Session() as s:
  u=await get_user(s,m.from_user.id); rows=(await s.execute(select(PayrollEntry).where(PayrollEntry.user_id==u.id).order_by(PayrollEntry.created_at.desc()).limit(20))).scalars().all(); await m.answer('\n'.join(f'{x.period}: {x.kind} {x.amount:.2f} ₽' for x in rows) or 'Выплат пока нет.')
@r.message(F.text=='👤 Профиль')
async def profile(m):
 async with Session() as s:
  u=await get_user(s,m.from_user.id); rs=await roles_of(s,u); await m.answer(f'👤 {u.name}\nID: {u.tg_id}\nРоли: {", ".join(ROLES[x] for x in rs)}')
