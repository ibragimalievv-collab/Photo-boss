from importlib import import_module

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import config

engine = create_async_engine(config.database_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def init_db():
    import_module('.models', package=__package__)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
