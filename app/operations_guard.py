"""Cross-process barrier between live operations and an archived reset.

Locks use a separate unpooled connection: a long file upload must not consume a
slot needed by the handler's own transaction and deadlock the application pool.
"""
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .miniapp_security import AccessError

ADVISORY_KEY = 7620182632501
GENERATION_KEY = 'operations:generation'
MAINTENANCE_KEY = 'operations:maintenance'
_lease_engines = WeakKeyDictionary()


class OperationsResetError(AccessError):
    def __init__(self, generation):
        super().__init__('Рабочие данные обнулены владельцем. Обновите приложение и начните действие заново.', 409)
        self.generation = generation


async def current_generation(conn):
    result = await conn.execute(text('SELECT value FROM settings WHERE key=:key'), {'key': GENERATION_KEY})
    return result.scalar() or ''


async def require_generation(conn, supplied):
    generation = await current_generation(conn)
    if generation and supplied != generation:
        raise OperationsResetError(generation)
    return generation


def _lease_engine(engine):
    if getattr(getattr(engine, 'dialect', None), 'name', None) != 'postgresql':
        return engine
    if engine not in _lease_engines:
        _lease_engines[engine] = create_async_engine(engine.url, poolclass=NullPool)
    return _lease_engines[engine]


@asynccontextmanager
async def operation_lease(engine):
    async with _lease_engine(engine).begin() as conn:
        if conn.dialect.name == 'postgresql':
            await conn.execute(text('SELECT pg_advisory_xact_lock_shared(:key)'), {'key': ADVISORY_KEY})
        maintenance = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                                          {'key': MAINTENANCE_KEY})).scalar()
        if maintenance:
            raise AccessError('Обновляются рабочие данные. Повторите действие через минуту.', 503)
        yield conn


async def close_operation_leases(engine):
    lease_engine = _lease_engines.pop(engine, None)
    if lease_engine is not None:
        await lease_engine.dispose()
