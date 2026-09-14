import os
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()
@dataclass(frozen=True)
class Config:
    bot_token: str=os.getenv('BOT_TOKEN','')
    database_url: str=os.getenv('DATABASE_URL','postgresql+asyncpg://postgres:postgres@localhost:5432/hotel_photo_bot')
    admin_ids: tuple[int,...]=tuple(int(x) for x in os.getenv('ADMIN_TELEGRAM_IDS','').split(',') if x.strip().isdigit())
    photo_price: float=float(os.getenv('PHOTO_PRICE','400'))
    manager_percent: float=float(os.getenv('MANAGER_PERCENT','15'))
    photographer_percent: float=float(os.getenv('PHOTOGRAPHER_PERCENT','10'))
config=Config()
