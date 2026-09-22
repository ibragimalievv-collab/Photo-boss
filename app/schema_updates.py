"""Additive, repeatable upgrades for databases created before the current release."""
from sqlalchemy import inspect, text


async def add_columns(connection, table, columns):
    existing = await connection.run_sync(
        lambda conn: {c['name'] for c in inspect(conn).get_columns(table)}
    )
    for name, definition in columns.items():
        if name not in existing:
            await connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))


async def upgrade(connection):
    tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
    if 'shootings' in tables:
        await add_columns(connection, 'shootings', {'viewing_at': 'TIMESTAMP'})
    if 'shoot_development_reviews' in tables:
        await add_columns(connection,'shoot_development_reviews',{'summary_parts':"TEXT NOT NULL DEFAULT '[]'"})
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
