import asyncio

from delivery_photo_migration_fixture import check_delivery_photo_set_upgrade
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
