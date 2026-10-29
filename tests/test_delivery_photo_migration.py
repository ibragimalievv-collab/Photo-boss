import asyncio

from delivery_photo_migration_fixture import (
    check_delivery_photo_set_upgrade,
    check_fresh_sale_lineage,
)
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


def test_legacy_photo_set_upgrade_preserves_data_and_is_repeatable():
    async def scenario():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        try:
            async with engine.begin() as connection:
                await check_delivery_photo_set_upgrade(connection)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_fresh_sale_lineage_defaults_and_old_column_restore():
    async def scenario():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        try:
            async with engine.begin() as connection:
                await check_fresh_sale_lineage(connection)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_startup_upgrades_legacy_delivery_before_creating_missing_sale_tables(monkeypatch):
    async def scenario():
        from app import db
        from app.audit_context import install_actor_context
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        install_actor_context(engine)
        monkeypatch.setattr(db, 'engine', engine)
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql('PRAGMA foreign_keys=ON')
                for sql in ('CREATE TABLE users(id INTEGER PRIMARY KEY)',
                            'CREATE TABLE delivery_galleries(id INTEGER PRIMARY KEY)',
                            '''CREATE TABLE delivery_photos(id INTEGER PRIMARY KEY,
                                gallery_id INTEGER REFERENCES delivery_galleries(id),
                                sha256 VARCHAR(64), UNIQUE(gallery_id,sha256))''',
                            'INSERT INTO delivery_galleries(id) VALUES(1)',
                            "INSERT INTO delivery_photos(id,gallery_id,sha256) VALUES(1,1,'retained')"):
                    await conn.execute(text(sql))
            await db.init_db()
            await db.init_db()
            async with engine.begin() as conn:
                assert await conn.scalar(text('SELECT sha256 FROM delivery_photos WHERE id=1')) == 'retained'
                foreign = await conn.run_sync(lambda c: inspect(c).get_foreign_keys('sale_draft_photos'))
                assert any(f['referred_table'] == 'delivery_photos' for f in foreign)
                assert not (await conn.exec_driver_sql('PRAGMA foreign_key_check')).all()
        finally:
            await engine.dispose()
    asyncio.run(scenario())
