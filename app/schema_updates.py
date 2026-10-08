"""Additive, repeatable upgrades for databases created before the current release."""
from sqlalchemy import MetaData, Table, UniqueConstraint, inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable


async def add_columns(connection, table, columns):
    existing = await connection.run_sync(
        lambda conn: {c['name'] for c in inspect(conn).get_columns(table)}
    )
    for name, definition in columns.items():
        if name not in existing:
            await connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))


def _delivery_photo_sets(connection):
    """Keep legacy selections while allowing the same original in two upload sets."""
    inspector = inspect(connection)
    constraints = inspector.get_unique_constraints('delivery_photos')
    indexes = inspector.get_indexes('delivery_photos')
    legacy_columns = {'gallery_id', 'sha256'}
    current_columns = legacy_columns | {'upload_set'}
    legacy_constraints = [c for c in constraints if set(c['column_names']) == legacy_columns]
    legacy_indexes = [i for i in indexes if i['unique'] and
                      set(i['column_names']) == legacy_columns and not i.get('duplicates_constraint')]
    current_exists = any(set(c['column_names']) == current_columns for c in constraints) or any(
        i['unique'] and set(i['column_names']) == current_columns for i in indexes
    )
    if not legacy_constraints and not legacy_indexes and current_exists:
        return

    quote = connection.dialect.identifier_preparer.quote
    if connection.dialect.name == 'postgresql':
        # Build the replacement before removing the old guarantee, in the same
        # startup transaction. Existing rows all received upload_set='ALL'.
        if not current_exists:
            connection.exec_driver_sql(
                'ALTER TABLE delivery_photos ADD CONSTRAINT uq_delivery_photo_set_digest '
                'UNIQUE (gallery_id, sha256, upload_set)'
            )
        for constraint in legacy_constraints:
            connection.exec_driver_sql(
                f'ALTER TABLE delivery_photos DROP CONSTRAINT {quote(constraint["name"])}'
            )
        for index in legacy_indexes:
            connection.exec_driver_sql(f'DROP INDEX {quote(index["name"])}')
    elif connection.dialect.name == 'sqlite':
        if legacy_constraints:
            # SQLite cannot drop a UNIQUE table constraint. Retain the full
            # reflected shape, including unknown additive columns and indexes.
            for table_name in inspector.get_table_names():
                if any(fk['referred_table'] == 'delivery_photos'
                       for fk in inspector.get_foreign_keys(table_name)):
                    raise RuntimeError('Cannot rebuild delivery_photos with incoming foreign keys')
            metadata = MetaData()
            original = Table('delivery_photos', metadata, autoload_with=connection)
            replacement = original.to_metadata(metadata, name='_delivery_photos_upload_set')
            for constraint in list(replacement.constraints):
                if isinstance(constraint, UniqueConstraint) and set(constraint.columns.keys()) == legacy_columns:
                    replacement.constraints.remove(constraint)
            if not current_exists:
                replacement.append_constraint(UniqueConstraint(
                    'gallery_id', 'sha256', 'upload_set', name='uq_delivery_photo_set_digest'
                ))
            triggers = connection.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='delivery_photos' AND sql IS NOT NULL"
            )).scalars().all()
            columns = ', '.join(quote(column.name) for column in original.columns)
            retained_indexes = [i for i in original.indexes if not (
                i.unique and set(i.columns.keys()) == legacy_columns
            )]
            # A SAVEPOINT also starts a physical transaction with sqlite's
            # legacy driver transaction mode, so table replacement is atomic.
            with connection.begin_nested():
                previous_count = connection.scalar(text('SELECT count(*) FROM delivery_photos'))
                connection.execute(CreateTable(replacement))
                connection.exec_driver_sql(
                    f'INSERT INTO _delivery_photos_upload_set ({columns}) SELECT {columns} FROM delivery_photos'
                )
                connection.exec_driver_sql('DROP TABLE delivery_photos')
                connection.exec_driver_sql('ALTER TABLE _delivery_photos_upload_set RENAME TO delivery_photos')
                for index in retained_indexes:
                    connection.execute(CreateIndex(index))
                for trigger in triggers:
                    connection.exec_driver_sql(trigger)
                if connection.scalar(text('SELECT count(*) FROM delivery_photos')) != previous_count:
                    raise RuntimeError('Delivery-photo migration changed the row count')
                if connection.exec_driver_sql('PRAGMA foreign_key_check(delivery_photos)').first():
                    raise RuntimeError('Delivery-photo migration violated foreign keys')
        else:
            for index in legacy_indexes:
                connection.exec_driver_sql(f'DROP INDEX {quote(index["name"])}')
            if not current_exists:
                connection.exec_driver_sql(
                    'CREATE UNIQUE INDEX uq_delivery_photo_set_digest '
                    'ON delivery_photos(gallery_id, sha256, upload_set)'
                )
    else:
        raise RuntimeError('Unsupported database for delivery-photo upload-set migration')


async def upgrade(connection):
    tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
    if 'shoot_development_reviews' in tables:
        await add_columns(connection,'shoot_development_reviews',{'summary_parts':"TEXT NOT NULL DEFAULT '[]'"})
    if 'delivery_galleries' in tables:
        await add_columns(connection, 'delivery_galleries', {'delivery_mode': "VARCHAR(20) NOT NULL DEFAULT 'ALL'"})
    if 'delivery_photos' in tables:
        await add_columns(connection, 'delivery_photos', {
            'selected': 'BOOLEAN NOT NULL DEFAULT FALSE',
            'upload_set': "VARCHAR(20) NOT NULL DEFAULT 'ALL'",
        })
        await connection.run_sync(_delivery_photo_sets)
    if 'sales' in tables:
        await add_columns(connection,'sales',{'manager_percent_applied':'VARCHAR(60)',
            'manager_payroll_entry_id':'INTEGER REFERENCES payroll_entries(id) ON DELETE SET NULL'})
    for table in ('shift_check_ins', 'shift_check_outs'):
        if table in tables:
            await add_columns(connection, table, {'offline_claimed_at': 'TIMESTAMP'})
    if 'shift_check_outs' in tables:
        await add_columns(connection, 'shift_check_outs', {'report_note': 'TEXT', 'report_saved_at': 'TIMESTAMP'})
    await add_columns(connection, 'notifications', {
        'event_key': 'VARCHAR(160)', 'priority': "VARCHAR(20) NOT NULL DEFAULT 'info'",
        'kind': "VARCHAR(50) NOT NULL DEFAULT 'legacy'", 'payload': 'TEXT',
        'acknowledged_at': 'TIMESTAMP', 'resolved_at': 'TIMESTAMP',
    })
    await connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_event ON notifications(user_id,event_key)'))
