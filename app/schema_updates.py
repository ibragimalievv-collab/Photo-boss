"""Additive, repeatable upgrades for databases created before the current release."""
import json
import logging
from decimal import Decimal

from sqlalchemy import MetaData, Table, UniqueConstraint, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateIndex, CreateTable

log = logging.getLogger(__name__)

# Money columns created as double precision by earlier releases (models.MONEY).
MONEY_COLUMNS = (
    ('packages', 'price_per_photo'), ('bookings', 'deposit'), ('sales', 'amount'),
    ('sales', 'commission'), ('compensation', 'base_salary'), ('payroll_entries', 'amount'),
    ('shift_check_ins', 'fine_amount'), ('sales_plans', 'target_amount'),
)
# Indexes for the daily lists, finance periods and history screens (see models).
PERFORMANCE_INDEXES = (
    'ix_photos_shooting_id', 'ix_bookings_shoot_date_status', 'ix_bookings_hotel_id_shoot_date',
    'ix_bookings_photographer_id_shoot_date', 'ix_bookings_manager_id_shoot_date',
    'ix_sales_booking_id', 'ix_sales_credited_user_id_created_at', 'ix_sales_created_at',
    'ix_payroll_entries_user_id_created_at', 'ix_payroll_entries_created_at',
    'ix_shifts_user_id_start_at', 'ix_shifts_start_at', 'ix_audit_logs_created_at',
)


async def add_columns(connection, table, columns):
    existing = await connection.run_sync(
        lambda conn: {c['name'] for c in inspect(conn).get_columns(table)}
    )
    for name, definition in columns.items():
        if name not in existing:
            await connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))


async def seed_default_unit_prices(connection):
    """Compatibility entry point: hotel prices require an explicit supervisor choice.

    Existing options are retained for history. Creating three alternatives here
    would leave the fixed hotel price ambiguous on every application restart.
    """
    return 0


async def seed_hotel_unit_prices(connection, hotel_id):
    """Do not invent a fixed price for a newly created hotel."""
    return 0


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


async def prepare_delivery_photos(connection):
    """Upgrade a legacy table before create_all can add an incoming lineage FK."""
    tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
    if 'delivery_photos' not in tables:
        return
    await add_columns(connection, 'delivery_photos', {
        'selected': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'upload_set': "VARCHAR(20) NOT NULL DEFAULT 'ALL'",
        'origin': "VARCHAR(16) NOT NULL DEFAULT 'UPLOAD'",
        'storage_identity': 'VARCHAR(64)',
    })
    await connection.run_sync(_delivery_photo_sets)


async def _postponable(connection, statement, label):
    """Run an optional upgrade step; never fail startup because of it.

    init_db limits lock waits to 2 s while the previous instance may still serve
    requests. A step that cannot get its lock is rolled back to its savepoint and
    retried at the next start; the application works either way.
    """
    try:
        async with connection.begin_nested():
            await connection.execute(text(statement) if isinstance(statement, str) else statement)
    except DBAPIError as exc:
        reason = str(getattr(exc, 'orig', exc)).splitlines()[0][:200]
        log.warning('Schema upgrade postponed until the next start (%s): %s', label, reason)
        return False
    return True


async def exact_money_and_indexes(connection, tables):
    from .db import Base

    busy = set()  # a table that is in use now: do not wait for it again in this start
    if connection.dialect.name == 'postgresql':
        for table, column in MONEY_COLUMNS:
            if table not in tables or table in busy:
                continue
            data_type = await connection.scalar(text(
                'SELECT data_type FROM information_schema.columns '
                'WHERE table_schema=current_schema() AND table_name=:table AND column_name=:column'),
                {'table': table, 'column': column})
            if data_type not in ('double precision', 'real'):
                continue
            # Values were written as rounded roubles and kopecks; round() keeps them exact.
            statement = (f'ALTER TABLE {table} ALTER COLUMN {column} '
                         f'TYPE NUMERIC(14,2) USING round({column}::numeric, 2)')
            if not await _postponable(connection, statement, f'{table}.{column}'):
                busy.add(table)
    indexes = {index.name: index for table in Base.metadata.tables.values() for index in table.indexes}
    for name in PERFORMANCE_INDEXES:
        index = indexes[name]
        if index.table.name not in tables or index.table.name in busy:
            continue
        if not await _postponable(connection, CreateIndex(index, if_not_exists=True), name):
            busy.add(index.table.name)


async def upgrade(connection):
    tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
    if 'hotels' in tables:
        await add_columns(connection, 'hotels', {'fixed_unit_tariff_id': 'INTEGER'})
    if 'photos' in tables:
        await add_columns(connection, 'photos', {'original_name': 'VARCHAR(255)', 'source_sha256': 'VARCHAR(64)'})
        await connection.execute(text('CREATE INDEX IF NOT EXISTS ix_photos_source_sha256 ON photos(source_sha256)'))
    if 'bookings' in tables:
        await add_columns(connection, 'bookings', {'pricing_snapshot': 'TEXT'})
        await add_columns(connection, 'bookings', {
            'no_sale_reason': 'VARCHAR(30)', 'no_sale_note': 'TEXT', 'no_sale_at': 'TIMESTAMP',
            'no_sale_by_id': 'INTEGER REFERENCES users(id)',
        })
    if 'shoot_development_reviews' in tables:
        await add_columns(connection,'shoot_development_reviews',{'summary_parts':"TEXT NOT NULL DEFAULT '[]'"})
    if 'delivery_galleries' in tables:
        await add_columns(connection, 'delivery_galleries', {
            'delivery_mode': "VARCHAR(20) NOT NULL DEFAULT 'ALL'",
            'auto_publish': 'BOOLEAN NOT NULL DEFAULT FALSE',
        })
    await prepare_delivery_photos(connection)
    # Historical SQLite delivery tables may require their unique-set rebuild.
    # Only add the incoming lineage FK after that rebuild has completed.
    if {'sale_draft_photos', 'delivery_photos'} <= tables:
        await add_columns(connection, 'sale_draft_photos', {
            'delivery_photo_id': 'INTEGER REFERENCES delivery_photos(id) ON DELETE SET NULL',
        })
        await connection.execute(text('CREATE INDEX IF NOT EXISTS ix_sale_draft_photos_delivery_photo_id '
                                      'ON sale_draft_photos(delivery_photo_id)'))
    if 'sales' in tables:
        await add_columns(connection, 'sales', {'pricing_snapshot': 'TEXT'})
        await add_columns(connection,'sales',{'manager_percent_applied':'VARCHAR(60)',
            'manager_payroll_entry_id':'INTEGER REFERENCES payroll_entries(id) ON DELETE SET NULL'})
    if 'sale_drafts' in tables:
        await add_columns(connection, 'sale_drafts', {'pricing_snapshot': 'TEXT'})
        # Freeze pre-upgrade counted drafts before administrators add hotel tariffs.
        # Legacy calculations used the booking's package unit price.
        if {'packages', 'bookings'} <= tables:
            rows = (await connection.execute(text('''SELECT d.id,d.sold_photos,d.declared_photo_count,
                b.hotel_id,p.name,p.price_per_photo FROM sale_drafts d
                JOIN bookings b ON b.id=d.booking_id JOIN packages p ON p.id=b.package_id
                WHERE d.pricing_snapshot IS NULL AND d.sold_photos>0
                AND d.status IN ('AWAITING_COUNTS','AWAITING_SELECTED')'''))).mappings().all()
            for row in rows:
                price = Decimal(str(row['price_per_photo'])).quantize(Decimal('0.01'))
                quote = {'tariffId': None, 'hotelId': row['hotel_id'], 'name': row['name'], 'kind': 'UNIT',
                    'price': str(price), 'photoCount': None, 'sold': row['sold_photos'],
                    'total': row['declared_photo_count'], 'amount': str(price*row['sold_photos'])}
                await connection.execute(text('UPDATE sale_drafts SET pricing_snapshot=:quote WHERE id=:id'),
                    {'id': row['id'], 'quote': json.dumps(quote, ensure_ascii=False)})
    if {'bookings', 'sales'} <= tables:
        await connection.execute(text("UPDATE bookings SET status='SOLD' WHERE status='READY_FOR_SALE' "
                                      "AND EXISTS (SELECT 1 FROM sales WHERE sales.booking_id=bookings.id)"))
    for table in ('shift_check_ins', 'shift_check_outs'):
        if table in tables:
            await add_columns(connection, table, {'offline_claimed_at': 'TIMESTAMP'})
    if 'shift_check_outs' in tables:
        await add_columns(connection, 'shift_check_outs', {'report_note': 'TEXT', 'report_saved_at': 'TIMESTAMP'})
    await seed_default_unit_prices(connection)
    await add_columns(connection, 'notifications', {
        'event_key': 'VARCHAR(160)', 'priority': "VARCHAR(20) NOT NULL DEFAULT 'info'",
        'kind': "VARCHAR(50) NOT NULL DEFAULT 'legacy'", 'payload': 'TEXT',
        'acknowledged_at': 'TIMESTAMP', 'resolved_at': 'TIMESTAMP',
    })
    await connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_event ON notifications(user_id,event_key)'))
    await exact_money_and_indexes(connection, tables)
    from .operational_reset import install_archive_guards
    await install_archive_guards(connection)
