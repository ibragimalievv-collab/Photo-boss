"""Large feed/count and concurrent same-event enqueue in disposable local schemas."""
import asyncio
import os
import time
from uuid import uuid4

from notification_feed_fixture import NOW, check_large_feed, check_scope
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Notification, Setting, User, UserRole
from app.push_notifications import queue_notice


async def concurrent_enqueue(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(User(id=1, tg_id=1, name='Synthetic producer'))
        await session.flush()
        session.add(UserRole(user_id=1, role='MANAGER'))
        await session.commit()
    barrier = asyncio.Barrier(2)
    async def produce(label):
        async with factory() as session:
            await barrier.wait()
            nid = await queue_notice(session, 1, kind='chat', event_key='same-booking-event',
                payload={'bookingId': 123}, message='Same committed event')
            session.add(Setting(key='synthetic-business:'+label, value='committed'))
            await asyncio.sleep(.03)
            await session.commit()
            return nid
    a, b = await asyncio.wait_for(asyncio.gather(produce('a'), produce('b')), 10)
    assert a == b
    async with factory() as session:
        assert await session.scalar(select(func.count(Notification.id))) == 1
        assert await session.scalar(select(func.count(Setting.key))) == 2
        # Distinct booking/payment milestones and unidentified legacy events
        # remain distinct even when their visible message text is the same.
        different = await queue_notice(session, 1, kind='chat', event_key='different-booking-event',
            payload={'bookingId': 456}, message='Same committed event')
        one = await queue_notice(session, 1, kind='chat', event_key=None, message='Same text')
        two = await queue_notice(session, 1, kind='chat', event_key=None, message='Same text')
        assert len({a, different, one, two}) == 4
        await session.commit()
    return {'sameEventRows': 1, 'committedBusinessTransactions': 2, 'distinctEvents': 4}


async def main():
    from app import push_notifications
    push_notifications.current_utc = lambda: NOW
    url = os.environ['UPDATE_POSTGRES_TEST_URL']
    assert make_url(url).host in {'localhost', '127.0.0.1'}, 'Only a local disposable database is allowed'
    admin = create_async_engine(url)
    try:
        for name, check in [('scope', check_scope), ('large-feed', check_large_feed), ('enqueue', concurrent_enqueue)]:
            schema = 'pb_notice_test_'+uuid4().hex
            async with admin.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
            try:
                started = time.monotonic()
                result = await check(engine)
                print(f'{name}: PASS {result or "scope parity and revocation"}; elapsed={time.monotonic()-started:.3f}s')
            finally:
                await engine.dispose()
                async with admin.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        await admin.dispose()


if __name__ == '__main__':
    asyncio.run(main())
