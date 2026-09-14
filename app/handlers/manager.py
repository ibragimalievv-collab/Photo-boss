from aiogram import Router,F
from ..db import Session
from sqlalchemy import select, func
from ..models import Booking, Hotel, Client, Shooting, Sale
from ..access import StaffFilter
from ..services.core import get_user
r=Router()
r.message.filter(StaffFilter("MANAGER"), F.text)
@r.message(F.text=='📋 Мои записи')
async def bookings(m):
 async with Session() as s:
  u=await get_user(s,m.from_user.id); rows=(await s.execute(select(Booking,Hotel,Client).join(Hotel,Hotel.id==Booking.hotel_id).join(Client,Client.id==Booking.client_id).where(Booking.manager_id==u.id).order_by(Booking.shoot_date.desc()).limit(30))).all()
  await m.answer('\n'.join(f'#{b.id} {h.name} / {c.name} / {b.shoot_date} {b.shoot_time} / {b.status}' for b,h,c in rows) or 'Записей нет.')
@r.message(F.text=='📸 Съёмки')
async def shootings(m):
 async with Session() as s:
  u=await get_user(s,m.from_user.id); rows=(await s.execute(select(Booking,Shooting).join(Shooting,Shooting.booking_id==Booking.id).where(Booking.manager_id==u.id))).all(); await m.answer('\n'.join(f'#{b.id}: {sh.status}' for b,sh in rows) or 'Съёмок нет.')
@r.message(F.text=='💰 Продажи')
async def sales(m):
 async with Session() as s:
  u=await get_user(s,m.from_user.id); rows=(await s.execute(select(Sale).where(Sale.created_by_id==u.id).order_by(Sale.created_at.desc()).limit(30))).scalars().all(); await m.answer('\n'.join(f'#{x.id}: {x.sold_photos} фото = {x.amount:.2f} ₽, засчитано #{x.credited_user_id}' for x in rows) or 'Продаж нет.')
@r.message(F.text=='🏆 Премия')
async def bonus(m): await m.answer('🏆 Премия: добавляется владельцем/администратором через раздел выплат.')
@r.message(F.text=='📊 Статистика')
async def manager_stats(m):
 async with Session() as s:
  u=await get_user(s,m.from_user.id); rev=(await s.execute(select(func.coalesce(func.sum(Sale.amount),0)).where(Sale.created_by_id==u.id))).scalar() or 0; await m.answer(f'📊 Оборот оформленных продаж: {rev:.2f} ₽')
