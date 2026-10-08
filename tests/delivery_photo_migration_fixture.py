"""Behavioral migration checks shared by SQLite and disposable PostgreSQL CI."""
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.schema_updates import upgrade


async def check_delivery_photo_set_upgrade(connection):
    if connection.dialect.name == 'sqlite':
        await connection.exec_driver_sql('PRAGMA foreign_keys=ON')
    for sql in (
        'CREATE TABLE users (id INTEGER PRIMARY KEY)',
        'CREATE TABLE delivery_galleries (id INTEGER PRIMARY KEY)',
        'CREATE TABLE notifications (id INTEGER PRIMARY KEY, user_id INTEGER)',
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
    originals = [dict(row) for row in (await connection.execute(
        text('SELECT * FROM delivery_photos ORDER BY id')
    )).mappings()]

    await upgrade(connection)
    await upgrade(connection)
    migrated = [dict(row) for row in (await connection.execute(
        text('SELECT * FROM delivery_photos ORDER BY id')
    )).mappings()]
    assert len(migrated) == 2
    assert all(row.pop('upload_set') == 'ALL' for row in migrated)
    assert migrated == originals, 'Legacy selections or original metadata changed'
    assert migrated[0]['selected'] and not migrated[1]['selected']

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
        ))).scalars().all() == [3, 7]
        assert not (await connection.exec_driver_sql('PRAGMA foreign_key_check')).all()
