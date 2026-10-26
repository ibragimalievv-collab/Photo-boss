"""Serialize notification producers without locking employees' user rows."""
from sqlalchemy import text

BACKGROUND_NOTIFICATIONS_KEY = 7620182632502


async def lock_background_notifications(session):
    # All background producers take this transaction-scoped lock before any
    # notification writes. It replaces recipient user-row locks: those create
    # a notification -> user inversion with attendance's user -> notification
    # order. A dedicated advisory lock cannot block employee shift actions,
    # including while an optional AI advice request is in flight.
    if session.bind.dialect.name == 'postgresql':
        await session.execute(text('SELECT pg_advisory_xact_lock(:key)'),
                              {'key': BACKGROUND_NOTIFICATIONS_KEY})
