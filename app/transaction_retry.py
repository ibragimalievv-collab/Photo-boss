"""Retry only transactions PostgreSQL has definitely rolled back.

Callers must open a fresh transaction for each attempt and must not send
non-idempotent external messages inside the retried operation.
"""
import asyncio
import logging

from sqlalchemy.exc import DBAPIError

log = logging.getLogger(__name__)
RETRYABLE_SQLSTATES = {'40P01', '40001'}


def transaction_sqlstate(error):
    """SQLAlchemy/asyncpg can wrap the server's SQLSTATE more than once."""
    seen, pending = set(), [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        state = getattr(current, 'sqlstate', None) or getattr(current, 'pgcode', None)
        if state in RETRYABLE_SQLSTATES:
            return state
        pending.extend(value for value in (
            getattr(current, 'orig', None), getattr(current, '__cause__', None),
            getattr(current, '__context__', None)) if value is not None)
    return None


async def retry_transaction(operation, *, attempts=3, delay=.05):
    for attempt in range(1, attempts + 1):
        try:
            result = await operation()
            if attempt > 1:
                log.info('Transaction recovered after retry; attempt %s/%s committed', attempt, attempts)
            return result
        except DBAPIError as error:
            state = transaction_sqlstate(error)
            if state is None or attempt == attempts:
                raise
            log.warning('Transaction rolled back (%s); retrying attempt %s/%s',
                        state, attempt + 1, attempts)
            await asyncio.sleep(delay * 2 ** (attempt - 1))
