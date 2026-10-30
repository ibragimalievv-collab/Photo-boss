"""An overlapping startup must roll back instead of indefinitely blocking traffic."""
import asyncio
import os
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app import db


async def main():
    schema='pb_startup_'+uuid4().hex
    engine=create_async_engine(os.environ['UPDATE_POSTGRES_TEST_URL'],pool_size=3,max_overflow=0,
        connect_args={'server_settings':{'search_path':schema}})
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        with patch.object(db,'engine',engine):
            await db.init_db()
            async with engine.begin() as old:
                await old.execute(text('LOCK TABLE work_chat_messages IN ACCESS SHARE MODE'))
                started=monotonic()
                try:
                    await db.init_db()
                except DBAPIError as error:
                    assert getattr(error.orig,'sqlstate',None)=='55P03',repr(error.orig)
                else:
                    raise AssertionError('Expected conflicting startup DDL to abort')
                assert monotonic()-started<8
                # The failed startup rolled back earlier booking/hotel locks.
                async with engine.connect() as reader:
                    await asyncio.wait_for(reader.execute(text('SELECT id FROM bookings LIMIT 1')),1)
            await db.init_db()
            async with engine.connect() as connection:
                assert await connection.scalar(text('SELECT 1'))==1
        print('PASS PostgreSQL: conflicting startup rolls back within lock deadline; ordinary booking reads recover; clean startup succeeds')
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()

if __name__=='__main__':asyncio.run(main())
