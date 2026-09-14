from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from ..models import User,UserRole,AuditLog,Compensation,Setting,Package,Hotel
ROLES={'OWNER':'👑 Владелец','ADMIN':'🛠 Администратор','MANAGER':'📋 Менеджер','PHOTOGRAPHER':'📸 Фотограф'}
def has(roles, role): return role in roles or 'OWNER' in roles or 'ADMIN' in roles
def menu(roles):
    x=['📸 Мои съёмки','🔄 Моя смена','📊 Моя статистика','💰 Мои выплаты','👤 Профиль'] if 'PHOTOGRAPHER' in roles else []
    if 'MANAGER' in roles: x += ['➕ Новая запись','📋 Мои записи','📸 Съёмки','💰 Продажи','📊 Статистика','🏆 Премия']
    if 'ADMIN' in roles or 'OWNER' in roles: x += ['👥 Сотрудники','🏨 Отели','📋 Все записи','📸 Все съёмки','💰 Продажи','💵 Зарплаты/выплаты','📊 Отчёты','📦 Пакеты','⚙️ Настройки','📜 Аудит']
    if any(r in roles for r in ('OWNER','ADMIN','MANAGER','PHOTOGRAPHER')): x += ['🧾 Продажа']
    return list(dict.fromkeys(x))
async def get_user(s,tg): return (await s.execute(select(User).where(User.tg_id==tg))).scalar_one_or_none()
async def roles_of(s,u): return {r.role for r in (await s.execute(select(UserRole).where(UserRole.user_id==u.id))).scalars()}
async def audit(s,u,action,entity=None,eid=None,details=None): s.add(AuditLog(user_id=u.id if u else None,action=action,entity=entity,entity_id=eid,details=details)); await s.commit()
async def setting(s,key,default):
    z=(await s.execute(select(Setting).where(Setting.key==key))).scalar_one_or_none(); return z.value if z else default
async def bootstrap(s,tg,name,username,owner=False):
    u=await get_user(s,tg)
    if not u: u=User(tg_id=tg,name=name,username=username); s.add(u); await s.flush()
    if owner or not (await roles_of(s,u)):
        role='OWNER' if owner else 'PHOTOGRAPHER'; s.add(UserRole(user_id=u.id,role=role))
    if owner and not (await s.execute(select(UserRole).where(UserRole.user_id==u.id,UserRole.role=='OWNER'))).scalar_one_or_none(): s.add(UserRole(user_id=u.id,role='OWNER'))
    if not (await s.execute(select(Package))).scalars().first(): s.add(Package(name='Базовая',price_per_photo=400))
    if not (await s.execute(select(Hotel))).scalars().first(): s.add(Hotel(name='Основной отель'))
    await s.commit(); return u
