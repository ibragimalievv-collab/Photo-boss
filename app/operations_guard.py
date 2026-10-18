"""Cross-process barrier between live operations and an archived reset.

Locks use a separate unpooled connection: a long file upload must not consume a
slot needed by the handler's own transaction and deadlock the application pool.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .miniapp_security import AccessError

ADVISORY_KEY = 7620182632501
GENERATION_KEY = 'operations:generation'
MAINTENANCE_KEY = 'operations:maintenance'
_lease_engines = WeakKeyDictionary()
_sqlite_locks = WeakKeyDictionary()


@dataclass(frozen=True)
class _SQLiteLeaseSnapshot:
    generation: str


class OperationsResetError(AccessError):
    def __init__(self, generation):
        super().__init__('Рабочие данные обнулены владельцем. Обновите приложение и начните действие заново.', 409)
        self.generation = generation


async def current_generation(conn):
    if isinstance(conn, _SQLiteLeaseSnapshot):
        return conn.generation
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


def _is_sqlite(engine):
    # The sync SQLite test adapter exposes its dialect through .inner.
    dialect = getattr(engine, 'dialect', None) or getattr(getattr(engine, 'inner', None), 'dialect', None)
    return getattr(dialect, 'name', None) == 'sqlite'


@asynccontextmanager
async def sqlite_operation_barrier(engine):
    """Serialize SQLite writers without holding a database read lock.

    PostgreSQL uses its cross-process advisory transaction locks. The SQLite
    mutex is shared by operational handlers, reset and restore; the latter
    keep their own database transaction locks for atomicity.
    """
    if not _is_sqlite(engine):
        yield
        return
    if engine not in _sqlite_locks:
        _sqlite_locks[engine] = asyncio.Lock()
    async with _sqlite_locks[engine]:
        yield


@asynccontextmanager
async def operation_lease(engine):
    if _is_sqlite(engine):
        async with sqlite_operation_barrier(engine):
            async with engine.connect() as conn:
                maintenance = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                                                  {'key': MAINTENANCE_KEY})).scalar()
                generation = await current_generation(conn)
            if maintenance:
                raise AccessError('Обновляются рабочие данные. Повторите действие через минуту.', 503)
            # A reader on another SQLite connection can block COMMIT. Release
            # it before the handler opens its own transaction; the mutex keeps
            # the cached generation stable until the handler has completed.
            yield _SQLiteLeaseSnapshot(generation)
        return
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
