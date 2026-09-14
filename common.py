from aiogram import Router,F
from aiogram.filters import CommandStart,Command
from aiogram.types import Message
from ..db import Session
from ..config import config
from ..services.core import *
from ..keyboards import reply
r=Router()
@r.message(CommandStart())
async def start(m:Message):
 async with Session() as s:
  u=await bootstrap(s,m.from_user.id,m.from_user.full_name,m.from_user.username,m.from_user.id in config.admin_ids)
  rs=await roles_of(s,u); await m.answer('🏨 Hotel Photo Bot\n\nРоли: '+', '.join(ROLES[x] for x in rs),reply_markup=reply(menu(rs)))
@r.message(Command('myid'))
async def myid(m): await m.answer(str(m.from_user.id))
