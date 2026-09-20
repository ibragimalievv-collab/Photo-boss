import asyncio
import logging
from importlib import import_module

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import config

logger = logging.getLogger(__name__)

engine_options = {"pool_pre_ping": True, "pool_recycle": 300}
if config.database_url.startswith("postgresql+asyncpg:"):
    engine_options["connect_args"] = {
        "timeout": 10,
        "server_settings": {"application_name": "photo-boss"},
    }

engine = create_async_engine(config.database_url, **engine_options)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def init_db():
    import_module(".models", package=__package__)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if connection.dialect.name == "postgresql":
            await connection.execute(
                text(
                    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS guest_count "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS deposit "
                    "DOUBLE PRECISION NOT NULL DEFAULT 0"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE work_chat_messages ADD COLUMN IF NOT EXISTS "
                    "attachment_id INTEGER"
                )
            )
            await connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_work_chat_messages_attachment_id "
                    "ON work_chat_messages(attachment_id) "
                    "WHERE attachment_id IS NOT NULL"
                )
            )
            await connection.execute(
                text(
                    """
                    DO $
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'fk_work_chat_message_attachment'
                              AND conrelid = 'work_chat_messages'::regclass
                        ) THEN
                            ALTER TABLE work_chat_messages
                            ADD CONSTRAINT fk_work_chat_message_attachment
                            FOREIGN KEY (attachment_id)
                            REFERENCES work_chat_attachments(id)
                            ON DELETE SET NULL;
                        END IF;
                    END $;
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    DO $
                    DECLARE item RECORD;
                    BEGIN
                        FOR item IN
                            SELECT conname FROM pg_constraint
                            WHERE conrelid = 'work_chat_messages'::regclass
                              AND contype = 'c'
                              AND pg_get_constraintdef(oid) ILIKE '%length(body)%'
                        LOOP
                            EXECUTE format(
                                'ALTER TABLE work_chat_messages DROP CONSTRAINT %I',
                                item.conname
                            );
                        END LOOP;
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'ck_work_chat_message_content'
                              AND conrelid = 'work_chat_messages'::regclass
                        ) THEN
                            ALTER TABLE work_chat_messages
                            ADD CONSTRAINT ck_work_chat_message_content
                            CHECK (
                                length(body) BETWEEN 0 AND 2000
                                AND (length(body) > 0 OR attachment_id IS NOT NULL)
                            );
                        END IF;
                    END $;
                    """
                )
            )
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
            if column_type == "integer":
                await connection.execute(
                    text("ALTER TABLE users ALTER COLUMN tg_id TYPE BIGINT")
                )
                logger.info("Migrated users.tg_id from INTEGER to BIGINT")


async def ping_database():
    async with engine.connect() as connection:
        return await connection.scalar(text("SELECT 1")) == 1


async def wait_for_database(attempts=None, delay=None):
    attempts = config.database_connect_attempts if attempts is None else attempts
    delay = config.database_connect_delay if delay is None else delay
    for attempt in range(1, attempts + 1):
        try:
            if await ping_database():
                logger.info("PostgreSQL connection is ready")
                return
        except (OSError, SQLAlchemyError) as exc:
            if attempt == attempts:
                logger.error(
                    "PostgreSQL unavailable after %s attempts (%s)",
                    attempts,
                    type(exc).__name__,
                )
                raise
            logger.warning(
                "Waiting for PostgreSQL: attempt %s/%s (%s)",
                attempt,
                attempts,
                type(exc).__name__,
            )
        await asyncio.sleep(delay)

    raise RuntimeError("PostgreSQL readiness check failed")
