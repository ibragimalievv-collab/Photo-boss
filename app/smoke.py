import asyncio
import logging

from sqlalchemy import text

from .config import config
from .db import engine, init_db, wait_for_database

logger = logging.getLogger(__name__)


async def main():
    logging.basicConfig(level=logging.INFO)
    try:
        config.validate()
        await wait_for_database()
        await init_db()
        async with engine.connect() as connection:
            database = await connection.scalar(text("SELECT current_database()"))
            column_type = await connection.scalar(
                text(
                    """
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'users'
                      AND column_name = 'tg_id'
                    """
                )
            )
        if column_type != "bigint":
            raise RuntimeError("users.tg_id must be BIGINT")
        logger.info("PostgreSQL smoke test passed for database %s", database)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
