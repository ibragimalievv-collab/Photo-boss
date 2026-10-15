"""Additive, repeatable upgrades for databases created before the current release."""
from sqlalchemy import MetaData, Table, UniqueConstraint, inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable


async def _map_legacy_attendance(connection):
    """Map historical daily rows only when a single planned shift is clear."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from .config import config
    tz = ZoneInfo(config.training_timezone)
    def utc(value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    plans = (await connection.execute(text("SELECT id,user_id,start_at,end_at FROM shifts WHERE status<>'CANCELLED'"))).mappings().all()
    by_day = {}
    for plan in plans:
        by_day.setdefault((plan['user_id'], str(utc(plan['start_at']).astimezone(tz).date())), []).append(plan)
    for table, timestamp in (('shift_check_ins', 'started_at'), ('shift_check_outs', 'ended_at')):
        rows = (await connection.execute(text(f'SELECT id,user_id,shift_date,{timestamp} FROM {table} WHERE scheduled_shift_id IS NULL'))).mappings().all()
        for row in rows:
            candidates = by_day.get((row['user_id'], str(row['shift_date'])), [])
            if len(candidates) != 1:
                when = utc(row[timestamp]) if row[timestamp] is not None else None
                candidates = [p for p in candidates if when is not None and utc(p['start_at']) <= when <= utc(p['end_at'])]
            if len(candidates) == 1:
                if table == 'shift_check_outs' and not await connection.scalar(text('''
                    SELECT id FROM shift_check_ins WHERE user_id=:uid AND shift_date=:day
                    AND scheduled_shift_id=:sid LIMIT 1'''),
                    {'uid': row['user_id'], 'day': row['shift_date'], 'sid': candidates[0]['id']}):
                    continue
                await connection.execute(text(f'''UPDATE {table} SET scheduled_shift_id=:sid WHERE id=:id
                    AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.user_id=:uid AND x.shift_date=:day AND x.scheduled_shift_id=:sid)'''),
                    {'sid': candidates[0]['id'], 'id': row['id'], 'uid': row['user_id'], 'day': row['shift_date']})


async def add_columns(connection, table, columns):
    existing = await connection.run_sync(
        lambda conn: {c['name'] for c in inspect(conn).get_columns(table)}
    )
    for name, definition in columns.items():
        if name not in existing:
            await connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))


def _scope_attendance(connection, table):
    """Preserve historical daily evidence while permitting distinct hotel shifts."""
    inspector = inspect(connection)
    old_columns = {'user_id', 'shift_date'}
    constraints = [c for c in inspector.get_unique_constraints(table)
                   if set(c['column_names']) == old_columns]
    indexes = [i for i in inspector.get_indexes(table) if i['unique'] and
               set(i['column_names']) == old_columns and
               i.get('dialect_options', {}).get('sqlite_where') is None and
               i.get('dialect_options', {}).get('postgresql_where') is None and
               not i.get('duplicates_constraint')]
    quote = connection.dialect.identifier_preparer.quote
    if connection.dialect.name == 'postgresql':
        for constraint in constraints:
            connection.exec_driver_sql(f'ALTER TABLE {quote(table)} DROP CONSTRAINT {quote(constraint["name"])}')
        for index in indexes:
            connection.exec_driver_sql(f'DROP INDEX {quote(index["name"])}')
    elif connection.dialect.name == 'sqlite' and constraints:
        metadata = MetaData()
        original = Table(table, metadata, autoload_with=connection)
        replacement = original.to_metadata(metadata, name=f'_{table}_scoped')
        for constraint in list(replacement.constraints):
            if isinstance(constraint, UniqueConstraint) and set(constraint.columns.keys()) == old_columns:
                replacement.constraints.remove(constraint)
        columns = ', '.join(quote(c.name) for c in original.columns)
        triggers = connection.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=:table AND sql IS NOT NULL"
        ), {'table': table}).scalars().all()
        retained_indexes = [i for i in original.indexes if i.name not in {x['name'] for x in indexes}]
        with connection.begin_nested():
            count = connection.scalar(text(f'SELECT count(*) FROM {quote(table)}'))
            connection.execute(CreateTable(replacement))
            connection.exec_driver_sql(f'INSERT INTO {quote(replacement.name)} ({columns}) SELECT {columns} FROM {quote(table)}')
            connection.exec_driver_sql(f'DROP TABLE {quote(table)}')
            connection.exec_driver_sql(f'ALTER TABLE {quote(replacement.name)} RENAME TO {quote(table)}')
            for index in retained_indexes:
                connection.execute(CreateIndex(index))
            for trigger in triggers:
                connection.exec_driver_sql(trigger)
            if connection.scalar(text(f'SELECT count(*) FROM {quote(table)}')) != count:
                raise RuntimeError('Attendance migration changed evidence count')
    elif connection.dialect.name == 'sqlite':
        for index in indexes:
            connection.exec_driver_sql(f'DROP INDEX {quote(index["name"])}')
    else:
        raise RuntimeError('Unsupported attendance database')
    connection.exec_driver_sql(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_user_day_plan '
                              f'ON {quote(table)}(user_id,shift_date,scheduled_shift_id)')
    connection.exec_driver_sql(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_unscheduled '
                              f'ON {quote(table)}(user_id,shift_date) WHERE scheduled_shift_id IS NULL')


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
    if 'hotels' in tables:
        await add_columns(connection, 'hotels', {'latitude': 'DOUBLE PRECISION',
            'longitude': 'DOUBLE PRECISION', 'attendance_radius_m': 'DOUBLE PRECISION'})
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
    if 'shootings' in tables:
        await add_columns(connection, 'shootings', {'processing_started_at': 'TIMESTAMP',
            'viewing_at': 'TIMESTAMP', 'viewing_comment': 'TEXT'})
    if 'payroll_entries' in tables:
        await add_columns(connection, 'payroll_entries', {'source_key': 'VARCHAR(160)'})
        await connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_payroll_entry_source_key ON payroll_entries(source_key)'))
    for table in ('shift_check_ins', 'shift_check_outs'):
        if table in tables:
            await add_columns(connection, table, {'offline_claimed_at': 'TIMESTAMP',
                'scheduled_shift_id': 'INTEGER REFERENCES shifts(id)', 'photo_data': 'TEXT',
                'location_accuracy': 'DOUBLE PRECISION'})
            await connection.run_sync(lambda conn, table=table: _scope_attendance(conn, table))
    if 'shift_check_ins' in tables:
        await add_columns(connection, 'shift_check_ins', {'ai_status': 'VARCHAR(30)',
            'ai_note': 'TEXT', 'ai_checked_at': 'TIMESTAMP'})
    if 'shift_check_outs' in tables:
        await add_columns(connection, 'shift_check_outs', {'report_note': 'TEXT', 'report_saved_at': 'TIMESTAMP'})
    if {'shift_check_ins', 'shift_check_outs', 'shifts', 'settings'} <= tables:
        marker = 'PHOTOGRAPHER_SCOPED_ATTENDANCE_MIGRATED'
        if not await connection.scalar(text('SELECT value FROM settings WHERE key=:key'), {'key': marker}):
            await _map_legacy_attendance(connection)
            await connection.execute(text("INSERT INTO settings(key,value) VALUES (:key,'1') ON CONFLICT(key) DO NOTHING"), {'key': marker})
    await add_columns(connection, 'notifications', {
        'event_key': 'VARCHAR(160)', 'priority': "VARCHAR(20) NOT NULL DEFAULT 'info'",
        'kind': "VARCHAR(50) NOT NULL DEFAULT 'legacy'", 'payload': 'TEXT',
        'acknowledged_at': 'TIMESTAMP', 'resolved_at': 'TIMESTAMP',
    })
    await connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_event ON notifications(user_id,event_key)'))
