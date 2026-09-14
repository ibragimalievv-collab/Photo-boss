import asyncio
from aiogram import Bot,Dispatcher
from .config import config
from .db import init_db
from .handlers import common,photographer,manager,admin,sales
async def main():
 if not config.bot_token: raise RuntimeError('BOT_TOKEN is required')
 await init_db(); bot=Bot(config.bot_token); dp=Dispatcher(); dp.include_routers(common.r,photographer.r,manager.r,sales.r,admin.r); await dp.start_polling(bot)
if __name__=='__main__': asyncio.run(main())
