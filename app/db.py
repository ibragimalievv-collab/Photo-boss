import asyncio
import logging
import os
from importlib import import_module

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from .config import config

logger = logging.getLogger(__name__)

engine_options = {"pool_pre_ping": True, "pool_recycle": 300}
if os.getenv("CI", "").lower() == "true":
    # Regression tests create a fresh event loop for each scenario. Avoid
    # handing an asyncpg connection created by one loop to another loop.
    engine_options["poolclass"] = NullPool
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
            await connection.execute(text(
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancellation_reason TEXT"
            ))
            await connection.execute(text(
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS "
                "cancelled_at TIMESTAMP WITHOUT TIME ZONE"
            ))
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
            fk_exists = await connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON tc.constraint_name = kcu.constraint_name
                         AND tc.constraint_schema = kcu.constraint_schema
                        WHERE tc.table_schema = current_schema()
                          AND tc.table_name = 'work_chat_messages'
                          AND tc.constraint_type = 'FOREIGN KEY'
                          AND kcu.column_name = 'attachment_id'
                    )
                    """
                )
            )
            if not fk_exists:
                await connection.execute(
                    text(
                        """
                        ALTER TABLE work_chat_messages
                        ADD CONSTRAINT fk_work_chat_message_attachment
                        FOREIGN KEY (attachment_id)
                        REFERENCES work_chat_attachments(id)
                        ON DELETE SET NULL
                        """
                    )
                )

            check_names = (
                await connection.execute(
                    text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid = 'work_chat_messages'::regclass
                          AND contype = 'c'
                          AND pg_get_constraintdef(oid) ILIKE '%length(body)%'
                        """
                    )
                )
            ).scalars().all()
            for name in check_names:
                if not name or not all(ch.isalnum() or ch == "_" for ch in name):
                    raise RuntimeError("Unexpected work-chat constraint name")
                await connection.exec_driver_sql(
                    f'ALTER TABLE work_chat_messages DROP CONSTRAINT "{name}"'
                )
            await connection.execute(
                text(
                    """
                    ALTER TABLE work_chat_messages
                    ADD CONSTRAINT ck_work_chat_message_content
                    CHECK (
                        length(body) BETWEEN 0 AND 2000
                        AND (length(body) > 0 OR attachment_id IS NOT NULL)
                    )
                    """
                )
            )
            await connection.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "terminated_at TIMESTAMP WITHOUT TIME ZONE"
            ))
            await connection.execute(text(
                "ALTER TABLE shootings ADD COLUMN IF NOT EXISTS "
                "ready_for_sale_at TIMESTAMP WITHOUT TIME ZONE"
            ))
            await connection.execute(text(
                "ALTER TABLE shootings ADD COLUMN IF NOT EXISTS "
                "full_upload_completed_at TIMESTAMP WITHOUT TIME ZONE"
            ))
            await connection.execute(text(
                "ALTER TABLE sales ADD COLUMN IF NOT EXISTS "
                "declared_photo_count INTEGER"
            ))
            await connection.execute(text(
                "ALTER TABLE sales ADD COLUMN IF NOT EXISTS "
                "source_draft_id INTEGER"
            ))
            await connection.execute(text(
                "ALTER TABLE sales ADD COLUMN IF NOT EXISTS "
                "commission_finalized_at TIMESTAMP WITHOUT TIME ZONE"
            ))
            await connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_sales_source_draft_id "
                "ON sales(source_draft_id) WHERE source_draft_id IS NOT NULL"
            ))
            await connection.execute(text(
                "ALTER TABLE training_assignments ADD COLUMN IF NOT EXISTS "
                "ai_score INTEGER"
            ))
            await connection.execute(text(
                "ALTER TABLE training_assignments ADD COLUMN IF NOT EXISTS "
                "ai_analysis TEXT"
            ))
            await connection.execute(text(
                "ALTER TABLE training_assignments ADD COLUMN IF NOT EXISTS "
                "review_source VARCHAR(20)"
            ))
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
