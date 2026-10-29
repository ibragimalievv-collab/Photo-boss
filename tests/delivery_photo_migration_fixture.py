"""Behavioral migration checks shared by SQLite and disposable PostgreSQL CI."""
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.schema_updates import upgrade


async def check_fresh_sale_lineage(connection):
    """Fresh databases and old-column restores use the same safe defaults."""
    from datetime import date, time

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db import Base
    from app.models import Booking, Client, Hotel, Package, SaleDraft, User

    if connection.dialect.name == 'sqlite':
        await connection.exec_driver_sql('PRAGMA foreign_keys=ON')
    await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        user = User(tg_id=123, name='Migration fixture')
        hotel = Hotel(name='Migration hotel')
        package = Package(name='Migration package', price_per_photo=400)
        guest = Client(name='Migration guest')
        session.add_all([user, hotel, package, guest])
        await session.flush()
        booking = Booking(hotel_id=hotel.id, client_id=guest.id, room='', deposit=0,
            shoot_date=date(2026, 10, 29), shoot_time=time(12), package_id=package.id,
            manager_id=user.id, photographer_id=user.id, status='READY_FOR_SALE')
        session.add(booking)
        await session.flush()
        draft = SaleDraft(booking_id=booking.id, created_by_id=user.id, status='AWAITING_RECEIPT')
        session.add(draft)
        await session.flush()
        bid, uid, did = booking.id, user.id, draft.id
    await connection.execute(text('''INSERT INTO delivery_galleries
        (id,booking_id,access_token,title,published,delivery_mode,created_by_id,created_at)
        VALUES (1,:bid,'fresh-default-token','Fresh',FALSE,'SELECTED',:uid,CURRENT_TIMESTAMP)'''),
        {'bid': bid, 'uid': uid})
    await connection.execute(text('''INSERT INTO delivery_photos
        (id,gallery_id,uploaded_by_id,filename,disk_path,sha256,byte_size,selected,created_at)
        VALUES (1,1,:uid,'fresh.jpg','app:/fresh.jpg','fresh-sha',321,FALSE,CURRENT_TIMESTAMP)'''), {'uid': uid})
    await connection.execute(text('''INSERT INTO sale_draft_photos
        (id,draft_id,telegram_file_id,telegram_unique_id,storage_path,sha256,byte_size,created_at)
        VALUES (1,:did,'app:/fresh.jpg','fresh-unique','app:/fresh.jpg','fresh-sha',321,CURRENT_TIMESTAMP)'''), {'did': did})
    assert not await connection.scalar(text('SELECT auto_publish FROM delivery_galleries WHERE id=1'))
    assert (await connection.execute(text('SELECT origin,upload_set,storage_identity FROM delivery_photos WHERE id=1'))).one() == ('UPLOAD', 'ALL', None)
    assert await connection.scalar(text('SELECT delivery_photo_id FROM sale_draft_photos WHERE id=1')) is None
    await connection.execute(text('UPDATE sale_draft_photos SET delivery_photo_id=1 WHERE id=1'))
    await upgrade(connection)
    await upgrade(connection)
    await connection.execute(text('DELETE FROM delivery_photos WHERE id=1'))
    assert await connection.scalar(text('SELECT delivery_photo_id FROM sale_draft_photos WHERE id=1')) is None
    assert await connection.scalar(text('SELECT storage_path FROM sale_draft_photos WHERE id=1')) == 'app:/fresh.jpg'


async def check_delivery_photo_set_upgrade(connection):
    if connection.dialect.name == 'sqlite':
        await connection.exec_driver_sql('PRAGMA foreign_keys=ON')
    for sql in (
        'CREATE TABLE users (id INTEGER PRIMARY KEY)',
        'CREATE TABLE delivery_galleries (id INTEGER PRIMARY KEY)',
        'CREATE TABLE notifications (id INTEGER PRIMARY KEY, user_id INTEGER)',
        '''CREATE TABLE sale_draft_photos (id INTEGER PRIMARY KEY,
            storage_path VARCHAR(500) NOT NULL UNIQUE, sha256 VARCHAR(64) NOT NULL,
            byte_size INTEGER NOT NULL)''',
        """CREATE TABLE delivery_photos (
            id INTEGER PRIMARY KEY,
            gallery_id INTEGER NOT NULL REFERENCES delivery_galleries(id) ON DELETE CASCADE,
            uploaded_by_id INTEGER NOT NULL REFERENCES users(id),
            filename VARCHAR(160) NOT NULL, disk_path VARCHAR(500) NOT NULL,
            sha256 VARCHAR(64) NOT NULL, byte_size INTEGER NOT NULL,
            selected BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMP NOT NULL,
            source_note TEXT DEFAULT 'legacy source',
            CONSTRAINT uq_delivery_photo_digest UNIQUE (gallery_id, sha256)
        )""",
        'CREATE INDEX ix_delivery_photos_gallery_id ON delivery_photos(gallery_id)',
        'CREATE INDEX ix_delivery_photos_filename ON delivery_photos(filename)',
        'INSERT INTO users(id) VALUES (1)',
        'INSERT INTO delivery_galleries(id) VALUES (1)',
        "INSERT INTO sale_draft_photos(id,storage_path,sha256,byte_size) VALUES (1,'app:/old-sale.jpg','old-sale',123)",
        """INSERT INTO delivery_photos
            (id,gallery_id,uploaded_by_id,filename,disk_path,sha256,byte_size,selected,created_at,source_note)
            VALUES (1,1,1,'selected.jpg','/original/selected.jpg','same-digest',321,TRUE,
                    '2026-10-08 10:00:00','keep selected original'),
                   (2,1,1,'all.jpg','/original/all.jpg','other-digest',456,FALSE,
                    '2026-10-08 10:01:00','keep all original')""",
    ):
        await connection.execute(text(sql))
    if connection.dialect.name == 'sqlite':
        await connection.execute(text('CREATE TABLE delivery_insert_events (photo_id INTEGER)'))
        await connection.execute(text("""CREATE TRIGGER delivery_photo_test_trigger
            AFTER INSERT ON delivery_photos BEGIN
                INSERT INTO delivery_insert_events(photo_id) VALUES (NEW.id);
            END"""))
    legacy_columns = ('id,gallery_id,uploaded_by_id,filename,disk_path,sha256,'
                      'byte_size,selected,created_at,source_note')
    originals = [dict(row) for row in (await connection.execute(
        text(f'SELECT {legacy_columns} FROM delivery_photos ORDER BY id')
    )).mappings()]

    await upgrade(connection)
    await upgrade(connection)
    migrated = [dict(row) for row in (await connection.execute(
        text(f'SELECT {legacy_columns},upload_set FROM delivery_photos ORDER BY id')
    )).mappings()]
    assert len(migrated) == 2
    assert all(row.pop('upload_set') == 'ALL' for row in migrated)
    assert migrated == originals, 'Legacy selections or original metadata changed'
    assert migrated[0]['selected'] and not migrated[1]['selected']
    assert not await connection.scalar(text('SELECT auto_publish FROM delivery_galleries WHERE id=1'))
    assert (await connection.execute(text('SELECT origin,storage_identity FROM delivery_photos WHERE id=1'))).one() == ('UPLOAD', None)
    assert await connection.scalar(text('SELECT delivery_photo_id FROM sale_draft_photos WHERE id=1')) is None

    async def copy_photo(photo_id, upload_set, digest='same-digest', gallery_id=1):
        await connection.execute(text("""INSERT INTO delivery_photos
            (id,gallery_id,uploaded_by_id,filename,disk_path,sha256,byte_size,selected,created_at,upload_set)
            SELECT :id,:gallery_id,uploaded_by_id,filename,disk_path,:digest,byte_size,
                   FALSE,created_at,:upload_set FROM delivery_photos WHERE id=1"""),
            {'id': photo_id, 'gallery_id': gallery_id, 'digest': digest, 'upload_set': upload_set})

    await copy_photo(3, 'SELECTED')
    assert (await connection.execute(text(
        "SELECT id,upload_set FROM delivery_photos WHERE sha256='same-digest' ORDER BY id"
    ))).all() == [(1, 'ALL'), (3, 'SELECTED')]
    for photo_id, upload_set in [(4, 'SELECTED'), (5, 'ALL')]:
        try:
            async with connection.begin_nested():
                await copy_photo(photo_id, upload_set)
        except IntegrityError:
            pass
        else:
            raise AssertionError('Duplicate digest accepted within the same upload set')
    try:
        async with connection.begin_nested():
            await copy_photo(6, 'SELECTED', digest='invalid-gallery', gallery_id=999)
    except IntegrityError:
        pass
    else:
        raise AssertionError('Gallery foreign key was lost during migration')

    await connection.execute(text("""INSERT INTO delivery_photos
        (id,gallery_id,uploaded_by_id,filename,disk_path,sha256,byte_size,selected,created_at)
        SELECT 7,gallery_id,uploaded_by_id,filename,disk_path,'server-default',byte_size,
               FALSE,created_at FROM delivery_photos WHERE id=1"""))
    assert await connection.scalar(text("SELECT upload_set FROM delivery_photos WHERE id=7")) == 'ALL'
    # An old restore omits every new field; defaults must remain closed/manual.
    await connection.execute(text('INSERT INTO delivery_galleries(id) VALUES (2)'))
    assert not await connection.scalar(text('SELECT auto_publish FROM delivery_galleries WHERE id=2'))
    assert await connection.scalar(text('SELECT origin FROM delivery_photos WHERE id=7')) == 'UPLOAD'
    # The new incoming FK is installed after SQLite's historical unique rebuild.
    await connection.execute(text('UPDATE sale_draft_photos SET delivery_photo_id=7 WHERE id=1'))
    try:
        async with connection.begin_nested():
            await connection.execute(text('UPDATE sale_draft_photos SET delivery_photo_id=999 WHERE id=1'))
    except IntegrityError:
        pass
    else:
        raise AssertionError('Sale gallery lineage accepts a missing photo')
    await connection.execute(text('DELETE FROM delivery_photos WHERE id=7'))
    assert await connection.scalar(text('SELECT delivery_photo_id FROM sale_draft_photos WHERE id=1')) is None
    await connection.execute(text("""INSERT INTO delivery_photos
        (id,gallery_id,uploaded_by_id,filename,disk_path,sha256,byte_size,selected,created_at)
        SELECT 7,gallery_id,uploaded_by_id,filename,disk_path,'server-default',byte_size,
               FALSE,created_at FROM delivery_photos WHERE id=1"""))
    await upgrade(connection)
    assert await connection.scalar(text('SELECT count(*) FROM delivery_photos')) == 4
    assert await connection.scalar(text('SELECT selected FROM delivery_photos WHERE id=1'))
    constraints, indexes, foreign_keys = await connection.run_sync(lambda conn: (
        inspect(conn).get_unique_constraints('delivery_photos'),
        inspect(conn).get_indexes('delivery_photos'),
        inspect(conn).get_foreign_keys('delivery_photos'),
    ))
    assert any(c['name'] == 'uq_delivery_photo_set_digest' and
               c['column_names'] == ['gallery_id', 'sha256', 'upload_set'] for c in constraints)
    assert not any(c['column_names'] == ['gallery_id', 'sha256'] for c in constraints)
    assert {'ix_delivery_photos_gallery_id', 'ix_delivery_photos_filename'} <= {i['name'] for i in indexes}
    assert len(foreign_keys) == 2
    if connection.dialect.name == 'sqlite':
        assert (await connection.execute(text(
            'SELECT photo_id FROM delivery_insert_events ORDER BY photo_id'
        ))).scalars().all() == [3, 7, 7]
        assert not (await connection.exec_driver_sql('PRAGMA foreign_key_check')).all()
