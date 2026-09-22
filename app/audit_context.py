"""Attribute transactionally captured row changes to the authenticated actor."""
from contextvars import ContextVar

from aiogram import BaseMiddleware
from sqlalchemy import event, select

actor_id = ContextVar('photo_boss_audit_actor', default=None)


def install_actor_context(engine):
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine.sync_engine, 'connect')
        def connect(connection, _record):
            state = {'actor': None}
            _record.info['pb_actor_state'] = state
            connection.create_function('pb_actor_id', 0, lambda: state['actor'])

        @event.listens_for(engine.sync_engine, 'before_cursor_execute')
        def sqlite_before(conn, _cursor, _statement, _parameters, _context, _many):
            conn.info['pb_actor_state']['actor'] = actor_id.get()
    elif engine.dialect.name == 'postgresql':
        @event.listens_for(engine.sync_engine, 'before_cursor_execute')
        def before(_conn, cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
                cursor.execute("SELECT set_config('photo_boss.actor', $1, true)", (str(actor_id.get() or ''),))


class ActorMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from .db import Session
        from .models import User
        user = data.get('event_from_user')
        uid = None
        if user is not None:
            async with Session() as session:
                uid = await session.scalar(select(User.id).where(User.tg_id == user.id))
        token = actor_id.set(uid)
        try:
            return await handler(event, data)
        finally:
            actor_id.reset(token)
