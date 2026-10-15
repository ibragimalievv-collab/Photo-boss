"""Legacy daily evidence survives the scheduled-hotel attendance upgrade."""
import asyncio
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.schema_updates import _scope_attendance, add_columns


def test_legacy_attendance_mapping_never_duplicates_time_fine_or_hotel_scope():
    from app.db import Base
    from app.models import Hotel, Shift, ShiftCheckIn, ShiftCheckOut, User
    from app.schema_updates import _map_legacy_attendance
    def when(day, hour):
        return datetime(2026, 10, day, hour, tzinfo=UTC).replace(tzinfo=None)
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with factory() as session:
                session.add_all([User(id=1, tg_id=1, name='Photographer'), Hotel(id=1, name='Hotel')])
                await session.flush()
                session.add_all([
                    Shift(id=1, user_id=1, hotel_id=1, start_at=when(15,6), end_at=when(15,15)),
                    Shift(id=2, user_id=1, hotel_id=1, start_at=when(16,6), end_at=when(16,10)),
                    Shift(id=3, user_id=1, hotel_id=1, start_at=when(16,11), end_at=when(16,15)),
                    Shift(id=4, user_id=1, hotel_id=1, start_at=when(17,6), end_at=when(17,10)),
                    Shift(id=5, user_id=1, hotel_id=1, start_at=when(17,11), end_at=when(17,15)),
                ])
                await session.flush()
                session.add_all([
                    ShiftCheckIn(id=1, user_id=1, shift_date=date(2026,10,15), status='STARTED', started_at=when(15,7), fine_amount=500, late=True),
                    ShiftCheckOut(id=1, user_id=1, shift_date=date(2026,10,15), status='FINISHED', ended_at=when(15,15)),
                    ShiftCheckIn(id=2, user_id=1, shift_date=date(2026,10,16), status='STARTED', started_at=when(16,7)),
                    ShiftCheckOut(id=2, user_id=1, shift_date=date(2026,10,16), status='FINISHED', ended_at=when(16,15)),
                    ShiftCheckIn(id=3, user_id=1, shift_date=date(2026,10,17), status='AWAITING_PHOTO'),
                ])
                await session.commit()
            async with engine.begin() as conn:
                await _map_legacy_attendance(conn)
                await _map_legacy_attendance(conn)
            async with factory() as session:
                first = await session.get(ShiftCheckIn, 1)
                assert first.scheduled_shift_id == 1
                assert first.started_at == when(15,7) and first.fine_amount == 500 and first.late
                assert (await session.get(ShiftCheckOut, 1)).scheduled_shift_id == 1
                assert (await session.get(ShiftCheckIn, 2)).scheduled_shift_id == 2
                assert (await session.get(ShiftCheckOut, 2)).scheduled_shift_id is None
                assert (await session.get(ShiftCheckIn, 3)).scheduled_shift_id is None
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('table', ['shift_check_ins', 'shift_check_outs'])
def test_scope_upgrade_preserves_evidence_and_enforces_both_unique_scopes(table):
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        try:
            async with engine.begin() as conn:
                await conn.execute(text('CREATE TABLE users(id INTEGER PRIMARY KEY)'))
                await conn.execute(text('CREATE TABLE shifts(id INTEGER PRIMARY KEY)'))
                await conn.execute(text('INSERT INTO users VALUES (1)'))
                await conn.execute(text('INSERT INTO shifts VALUES (10),(11)'))
                await conn.execute(text(f'''CREATE TABLE {table} (
                    id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                    shift_date DATE NOT NULL, evidence TEXT,
                    UNIQUE(user_id,shift_date))'''))
                await conn.execute(text(f'CREATE INDEX legacy_{table}_evidence ON {table}(evidence)'))
                await conn.execute(text('CREATE TABLE evidence_changes(row_id INTEGER)'))
                await conn.execute(text(f'''CREATE TRIGGER capture_{table} AFTER UPDATE ON {table}
                    BEGIN INSERT INTO evidence_changes VALUES (new.id); END'''))
                await conn.execute(text(f"INSERT INTO {table} VALUES (1,1,'2026-10-15','original private photo reference')"))
                await add_columns(conn, table, {'scheduled_shift_id': 'INTEGER REFERENCES shifts(id)'})
                for _ in range(2):
                    await conn.run_sync(lambda c: _scope_attendance(c, table))
                original = (await conn.execute(text(f'SELECT evidence,scheduled_shift_id FROM {table} WHERE id=1'))).one()
                assert original == ('original private photo reference', None)
                await conn.execute(text(f"INSERT INTO {table}(id,user_id,shift_date,evidence,scheduled_shift_id) VALUES (2,1,'2026-10-15','hotel one',10),(3,1,'2026-10-15','hotel two',11)"))
                await conn.execute(text(f"UPDATE {table} SET evidence='new reference' WHERE id=3"))
                assert await conn.scalar(text('SELECT row_id FROM evidence_changes')) == 3
                assert await conn.scalar(text(f'SELECT count(*) FROM {table}')) == 3
                for sid in (None, 10):
                    with pytest.raises(IntegrityError):
                        async with conn.begin_nested():
                            await conn.execute(text(f"INSERT INTO {table}(id,user_id,shift_date,scheduled_shift_id) VALUES (4,1,'2026-10-15',:sid)"), {'sid': sid})
                assert await conn.scalar(text(f"SELECT count(*) FROM sqlite_master WHERE type='index' AND name='legacy_{table}_evidence'")) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
