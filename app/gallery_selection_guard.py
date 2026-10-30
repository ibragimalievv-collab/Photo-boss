"""Serialize a booking's selected-folder mutations outside the application pool."""
import asyncio
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary, WeakValueDictionary

from sqlalchemy import text

from .miniapp_security import AccessError
from .operations_guard import _lease_engine

_slots = WeakKeyDictionary()
_locks = WeakKeyDictionary()


@asynccontextmanager
async def selection_guard(engine, booking_id):
    if not 0 < booking_id < 2**31:
        raise AccessError('Некорректная запись.', 400)
    if engine not in _slots:
        _slots[engine] = asyncio.Semaphore(4)
    slots = _slots[engine]
    try:
        await asyncio.wait_for(slots.acquire(), 2)
    except TimeoutError as exc:
        raise AccessError('Выбор фотографий ещё сохраняется. Повторите через несколько секунд.', 503) from exc
    try:
        if engine.dialect.name == 'postgresql':
            async with _lease_engine(engine).begin() as connection:
                # Separate advisory namespace; both select and deselect serialize
                # across overlapping Render instances without occupying pooled slots.
                await connection.execute(text("SET LOCAL lock_timeout = '3s'"))
                await connection.execute(text('SELECT pg_advisory_xact_lock(762019, :bid)'), {'bid': booking_id})
                yield
        else:
            if engine not in _locks:
                _locks[engine] = WeakValueDictionary()
            lock = _locks[engine].setdefault(booking_id, asyncio.Lock())
            async with lock:
                yield
    finally:
        slots.release()
