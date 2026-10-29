"""Atomic, recoverable reset of operational ledgers, never a database wipe.

No remote/local media is deleted. The append-only snapshot includes reflected
columns, typed values, primary keys and detached Academy links. Production
PostgreSQL sequences are deliberately untouched, so old media paths are not reused.
"""
import base64
import hashlib
import json
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import MetaData, and_, func, inspect, select, text

from .operations_guard import sqlite_operation_barrier

GENERATION_KEY = 'operations:generation'
RESET_AT_KEY = 'operations:reset_at'
ARCHIVE_TABLE = 'operational_reset_archives'
RESTORE_TABLE = 'operational_reset_restores'
_UNSET = object()
# Child-before-parent order; explicit deletes also capture rows hidden by CASCADE.
DELETE_TABLES = (
    'bank_payments', 'work_breaks',
    'guest_feedback', 'bank_reconciliations', 'delivery_booking_responses',
    'delivery_reminders', 'delivery_claims', 'delivery_photos', 'delivery_galleries',
    'photo_edits', 'photo_storage', 'shoot_development_reviews', 'photos',
    'sale_draft_photos', 'sales', 'receipts', 'sale_drafts', 'sale_discount_approvals', 'booking_reminders',
    'repeat_sale_leads', 'shootings', 'bookings', 'shift_check_ins',
    'shift_check_outs', 'work_checklist_completions',
)
OPERATIONAL_NOTIFICATION_KINDS = (
    'control', 'daily_summary', 'manager_idle', 'manager_advice',
    'sale_discount', 'attendance',
)
OPERATIONAL_SETTING_PREFIXES = (
    'booking:trash:', 'booking-control:advice:', 'gallery:meta:',
    'gallery:project-fav:', 'gallery:photo-fav:', 'gallery:views:',
    'gallery:downloads:', 'notify:last-critical:', 'shoot-audit:',
)


class ResetError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _encode(value):
    # Explicit tags on every cell prevent collisions with native JSON objects.
    if value is None:
        return ['null', None]
    if isinstance(value, bool):
        return ['bool', value]
    if isinstance(value, int):
        return ['int', value]
    if isinstance(value, float):
        return ['float', value.hex()]
    if isinstance(value, str):
        return ['str', value]
    if isinstance(value, datetime):
        return ['datetime', value.isoformat()]
    if isinstance(value, date):
        return ['date', value.isoformat()]
    if isinstance(value, time):
        return ['time', value.isoformat()]
    if isinstance(value, Decimal):
        return ['decimal', str(value)]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ['bytes', base64.b64encode(value).decode('ascii')]
    if isinstance(value, UUID):
        return ['uuid', str(value)]
    if isinstance(value, (dict, list)):
        return ['json', value]
    raise ResetError(f'Unsupported archive value type: {type(value).__name__}')


def _decode(value):
    kind, data = value
    convert = {'null': lambda _: None, 'bool': bool, 'int': int, 'float': float.fromhex,
               'str': str, 'datetime': datetime.fromisoformat, 'date': date.fromisoformat,
               'time': time.fromisoformat, 'decimal': Decimal, 'bytes': base64.b64decode,
               'uuid': UUID, 'json': lambda item: item}
    if kind not in convert:
        raise ResetError('Unknown archive value type')
    return convert[kind](data)


def _pack(table, rows):
    columns = list(table.columns.keys())
    return {'columns': columns, 'rows': [[_encode(row[name]) for name in columns] for row in rows]}


def _unpack(block):
    return [dict(zip(block['columns'], (_decode(value) for value in row), strict=True))
            for row in block['rows']]


def _restored_blocks_match(current, archived):
    """Compare all archived cells; only reviewed new lineage defaults may differ."""
    safe_defaults = {'delivery_galleries': {'auto_publish': False},
                     'delivery_photos': {'origin': 'UPLOAD', 'storage_identity': None},
                     'sale_draft_photos': {'delivery_photo_id': None}}
    normalized = {}
    for name, block in current.items():
        previous = archived.get(name)
        if previous is None:
            if block['rows']:
                return False
            continue
        added = set(block['columns']) - set(previous['columns'])
        allowed = safe_defaults.get(name, {})
        if not added.issubset(allowed) or not set(previous['columns']).issubset(block['columns']):
            return False
        for row in _unpack(block):
            if any(row[column] != allowed[column] for column in added):
                return False
        indexes = [block['columns'].index(column) for column in previous['columns']]
        normalized[name] = {'columns': previous['columns'],
                            'rows': [[row[index] for index in indexes] for row in block['rows']]}
    return normalized == archived


async def install_archive_guards(conn):
    """Database-enforced append-only archives, including direct/raw SQL writes."""
    tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    if ARCHIVE_TABLE not in tables:
        return
    if conn.dialect.name == 'postgresql':
        await conn.exec_driver_sql("""CREATE OR REPLACE FUNCTION pb_reset_archive_immutable()
            RETURNS trigger AS $$ BEGIN
            RAISE EXCEPTION 'Operational reset archives are immutable';
            END; $$ LANGUAGE plpgsql""")
        await conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS pb_reset_archive_immutable ON {ARCHIVE_TABLE}')
        await conn.exec_driver_sql(f'''CREATE TRIGGER pb_reset_archive_immutable
            BEFORE UPDATE OR DELETE ON {ARCHIVE_TABLE} FOR EACH ROW
            EXECUTE FUNCTION pb_reset_archive_immutable()''')
        await conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS pb_reset_archive_no_truncate ON {ARCHIVE_TABLE}')
        await conn.exec_driver_sql(f'''CREATE TRIGGER pb_reset_archive_no_truncate
            BEFORE TRUNCATE ON {ARCHIVE_TABLE} FOR EACH STATEMENT
            EXECUTE FUNCTION pb_reset_archive_immutable()''')
    elif conn.dialect.name == 'sqlite':
        for verb in ('UPDATE', 'DELETE'):
            await conn.exec_driver_sql(f'''CREATE TRIGGER IF NOT EXISTS pb_reset_archive_{verb.lower()}
                BEFORE {verb} ON {ARCHIVE_TABLE} BEGIN
                SELECT RAISE(ABORT, 'Operational reset archives are immutable'); END''')
    else:
        raise ResetError('Unsupported database for a verified operational reset')


async def _reflect(conn):
    metadata = MetaData()
    await conn.run_sync(metadata.reflect)
    return metadata.tables


async def _lock(conn, tables):
    if conn.dialect.name == 'postgresql':
        await conn.execute(text('SELECT pg_advisory_xact_lock(7620182632501)'))
        # Includes dynamically declared tables, FK parents, all audit triggers and
        # in-flight bot operations. Locks release only at commit/rollback.
        quote = conn.dialect.identifier_preparer.quote
        names = ', '.join(quote(name) for name in sorted(tables))
        await conn.exec_driver_sql(f'LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE')
    elif conn.dialect.name == 'sqlite':
        # A real write transaction starts before the first SELECT even with the
        # sqlite driver's legacy transaction mode.
        await conn.exec_driver_sql('BEGIN IMMEDIATE')
    else:
        raise ResetError('Unsupported database for an atomic operational reset')


async def _owner(conn, tables, owner_id):
    users, roles = tables['users'], tables['user_roles']
    allowed = await conn.scalar(select(users.c.id).join(roles, roles.c.user_id == users.c.id).where(
        users.c.id == owner_id, users.c.active.is_(True), roles.c.role == 'OWNER'))
    if allowed is None:
        raise ResetError('An active owner is required for an operational reset')


async def _rows(conn, table, condition=None):
    statement = select(table)
    if condition is not None:
        statement = statement.where(condition)
    primary = list(table.primary_key.columns)
    if not primary:
        raise ResetError(f'Table {table.name} has no primary key; reset refused')
    return list((await conn.execute(statement.order_by(*primary))).mappings())


def _ident(table, row):
    return and_(*(column == row[column.name] for column in table.primary_key.columns))


async def _snapshot(conn, tables):
    missing = set(DELETE_TABLES) - set(tables)
    if missing:
        raise ResetError('Operational schema is incomplete: ' + ', '.join(sorted(missing)))
    # New dependency tables require an explicit decision, never silent cascading.
    for table in tables.values():
        for foreign in table.foreign_keys:
            known_detachment = (table.name == 'academy_reviews' and foreign.parent.name == 'photo_id'
                                and foreign.column.table.name == 'photos' and foreign.parent.nullable)
            if (foreign.column.table.name in DELETE_TABLES and table.name not in DELETE_TABLES
                    and not known_detachment):
                raise ResetError(f'Unreviewed dependency: {table.name} -> {foreign.column.table.name}')
    deleted = {name: await _rows(conn, tables[name]) for name in DELETE_TABLES}
    sale_ids = {row['id'] for row in deleted['sales']}
    booking_ids = {row['id'] for row in deleted['bookings']}
    payroll_ids = {row['manager_payroll_entry_id'] for row in deleted['sales']
                   if row['manager_payroll_entry_id'] is not None}
    payroll = await _rows(conn, tables['payroll_entries'])
    def derived_payroll(row):
        if 'штраф' in row['kind'].casefold() or row['id'] in payroll_ids:
            return True
        if row['kind'] != 'Комиссия менеджера':
            return False
        links = dict(re.findall(r'(sale|booking)=(\d+)(?=;|\s|$)', row['note'] or ''))
        return int(links.get('sale', -1)) in sale_ids or int(links.get('booking', -1)) in booking_ids
    deleted['payroll_entries'] = [row for row in payroll if derived_payroll(row)]
    settings = tables['settings']
    deleted['settings'] = [row for row in await _rows(conn, settings)
                           if row['key'].startswith(OPERATIONAL_SETTING_PREFIXES)]
    notifications = tables['notifications']
    notice_rows = await _rows(conn, notifications, notifications.c.kind.in_(OPERATIONAL_NOTIFICATION_KINDS))
    if 'push_deliveries' in tables:
        delivery = tables['push_deliveries']
        deleted['push_deliveries'] = await _rows(conn, delivery, delivery.c.notification_id.in_([n['id'] for n in notice_rows]))
    deleted['notifications'] = notice_rows
    academy = tables['academy_reviews']
    detached = await _rows(conn, academy, academy.c.photo_id.is_not(None))
    previous_epoch = await _rows(conn, settings, settings.c.key.in_(
        (GENERATION_KEY, RESET_AT_KEY, 'operations:maintenance')))
    preserved = {}
    excluded = set(DELETE_TABLES) | {ARCHIVE_TABLE, RESTORE_TABLE}
    for name, table in sorted(tables.items()):
        if name in excluded:
            continue
        rows = await _rows(conn, table)
        if name in deleted:
            excluded_keys = {tuple(row[column.name] for column in table.primary_key.columns)
                             for row in deleted[name]}
            rows = [row for row in rows if tuple(row[column.name] for column in table.primary_key.columns)
                    not in excluded_keys]
        if name == 'settings':
            rows = [row for row in rows if row['key'] not in
                    (GENERATION_KEY, RESET_AT_KEY, 'operations:maintenance')]
        if name == 'academy_reviews':
            rows = [dict(row, photo_id=None) for row in rows]
        preserved[name] = {'count': len(rows), 'sha256': _digest(_canonical(_pack(table, rows)))}
        if name == 'audit_logs':
            preserved[name]['max_id'] = max((row['id'] for row in rows), default=0)
    return {'deleted': {name: _pack(tables[name], rows) for name, rows in deleted.items()},
            'detached_academy_reviews': _pack(academy, detached),
            'previous_epoch': _pack(settings, previous_epoch), 'preserved_manifest': preserved}


async def _verify_preserved(conn, tables, before, after):
    for name, expected in before['preserved_manifest'].items():
        if name == 'audit_logs':
            table = tables[name]
            rows = await _rows(conn, table, table.c.id <= expected['max_id'])
            actual = {'count': len(rows), 'sha256': _digest(_canonical(_pack(table, rows))),
                      'max_id': max((row['id'] for row in rows), default=0)}
        else:
            actual = after['preserved_manifest'].get(name)
        if actual != expected:
            raise ResetError(f'Preservation verification failed for {name}; transaction rolled back')


def _summary(snapshot):
    counts = {name: len(block['rows']) for name, block in snapshot['deleted'].items()}
    return {'row_counts': counts, 'total_deleted_rows': sum(counts.values()),
            'academy_links_detached': len(snapshot['detached_academy_reviews']['rows']),
            # Unrelated chat/Academy/auth writes between preview and reset do
            # not invalidate the operational preview. Their preservation is
            # verified independently under the reset's exclusive table locks.
            'snapshot_sha256': _digest(_canonical({key: value for key, value in snapshot.items()
                                                   if key != 'preserved_manifest'})),
            'preserved_manifest': snapshot['preserved_manifest'],
            'preserved': ['employees', 'roles', 'hotels', 'planned_shifts', 'prices_and_settings',
                          'manual_unrelated_payroll', 'cash_disbursements', 'clients', 'academy',
                          'hr', 'chats', 'operation_requests', 'audit_logs', 'physical_files']}


async def preview(engine):
    """Read-only counts and fingerprint; contains no guest, employee or secret data."""
    async with engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            await conn.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        tables = await _reflect(conn)
        snapshot = await _snapshot(conn, tables)
        return _summary(snapshot)


async def _set_epoch(conn, table, generation, reset_at):
    for key, value in ((GENERATION_KEY, generation), (RESET_AT_KEY, reset_at)):
        if await conn.scalar(select(table.c.key).where(table.c.key == key)) is None:
            await conn.execute(table.insert().values(key=key, value=value))
        else:
            await conn.execute(table.update().where(table.c.key == key).values(value=value))


def _archive_result(row):
    document = json.loads(row['payload'])
    return {'request_id': row['request_id'], 'archive_id': row['id'], 'generation': row['generation'],
            'reset_at': row['created_at'].replace(tzinfo=UTC).isoformat(),
            'archive_sha256': row['payload_sha256'], 'row_counts': json.loads(row['row_counts']),
            'preserved_manifest': document.get('preserved_manifest', {})}


async def _verified_archive(conn, table, request_id):
    row = (await conn.execute(select(table).where(table.c.request_id == request_id))).mappings().first()
    if row is not None and _digest(row['payload']) != row['payload_sha256']:
        raise ResetError('Archive checksum verification failed')
    return row


async def reset(engine, request_id, owner_id, *, expected_preview_sha256=None, expected_generation=_UNSET):
    """Archive and delete in one transaction; retry never touches newer records."""
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
        raise ResetError('A stable, unique reset request ID is required')
    from .audit_context import actor_id
    token = actor_id.set(owner_id)
    try:
        async with sqlite_operation_barrier(engine), engine.begin() as conn:
            tables = await _reflect(conn)
            await _lock(conn, tables)
            await _owner(conn, tables, owner_id)
            archive_table = tables[ARCHIVE_TABLE]
            previous = await _verified_archive(conn, archive_table, request_id)
            if previous is not None:
                return _archive_result(previous) | {'replayed': True}
            if expected_generation is not _UNSET:
                current_generation = await conn.scalar(select(tables['settings'].c.value).where(
                    tables['settings'].c.key == GENERATION_KEY))
                if (current_generation or '') != (expected_generation or ''):
                    raise ResetError('Operational generation changed; reset refused')
            maintenance = await conn.scalar(select(tables['settings'].c.value).where(
                tables['settings'].c.key == 'operations:maintenance'))
            if maintenance is not None and maintenance != request_id:
                raise ResetError('Another operational reset is in maintenance; reset refused')
            await install_archive_guards(conn)
            snapshot = await _snapshot(conn, tables)
            summary = _summary(snapshot)
            if expected_preview_sha256 is not None and expected_preview_sha256 != summary['snapshot_sha256']:
                raise ResetError('Operations changed after the reviewed preview; review a fresh preview')
            now, generation = datetime.now(UTC).replace(tzinfo=None), str(uuid4())
            payload = _canonical({'version': 1, 'request_id': request_id, 'generation': generation,
                                  'owner_id': owner_id, 'created_at': now.isoformat(), **snapshot})
            digest = _digest(payload)
            result = await conn.execute(archive_table.insert().values(
                request_id=request_id, owner_id=owner_id, created_at=now, generation=generation,
                payload=payload, payload_sha256=digest, row_counts=_canonical(summary['row_counts'])))
            archive_id = result.inserted_primary_key[0]
            # Read-back verifies actual persisted bytes before deleting any row.
            saved = await _verified_archive(conn, archive_table, request_id)
            if saved is None or saved['payload'] != payload:
                raise ResetError('Archive persistence verification failed')
            academy = tables['academy_reviews']
            await conn.execute(academy.update().where(academy.c.photo_id.is_not(None)).values(photo_id=None))
            for name, block in snapshot['deleted'].items():
                table, rows = tables[name], _unpack(block)
                if name in DELETE_TABLES:
                    await conn.execute(table.delete())
                else:
                    for row in rows:
                        await conn.execute(table.delete().where(_ident(table, row)))
            await _set_epoch(conn, tables['settings'], generation, now.replace(tzinfo=UTC).isoformat())
            await conn.execute(tables['settings'].delete().where(
                tables['settings'].c.key == 'operations:maintenance'))
            after = await _snapshot(conn, tables)
            remaining = _summary(after)
            if remaining['total_deleted_rows'] or remaining['academy_links_detached']:
                raise ResetError('Operational reset verification failed; transaction rolled back')
            await _verify_preserved(conn, tables, snapshot, after)
            await conn.execute(tables['audit_logs'].insert().values(user_id=owner_id,
                action='operations.reset', entity='operational_reset_archives', entity_id=archive_id,
                details=_canonical({'request_id': request_id, 'archive_sha256': digest,
                                    'row_counts': summary['row_counts'], 'generation': generation}), created_at=now))
            return _archive_result(saved) | {'replayed': False}
    finally:
        actor_id.reset(token)


async def inspect_archive(engine, request_id):
    """Verify and report archive metadata without returning private archived rows."""
    async with engine.connect() as conn:
        tables = await _reflect(conn)
        row = await _verified_archive(conn, tables[ARCHIVE_TABLE], request_id)
        if row is None:
            raise ResetError('Operational reset archive not found')
        return _archive_result(row)


async def restore(engine, request_id, owner_id):
    """Restore exact IDs/typed values only into a clean epoch; never overwrite new work.

    The caller must stop mutations first. New operational rows or changes to a
    detached Academy review reject the entire restoration with no partial writes.
    """
    from .audit_context import actor_id
    token = actor_id.set(owner_id)
    try:
        async with sqlite_operation_barrier(engine), engine.begin() as conn:
            tables = await _reflect(conn)
            await _lock(conn, tables)
            await _owner(conn, tables, owner_id)
            row = await _verified_archive(conn, tables[ARCHIVE_TABLE], request_id)
            if row is None:
                raise ResetError('Operational reset archive not found')
            restored = tables[RESTORE_TABLE]
            receipt = (await conn.execute(select(restored).where(restored.c.archive_id == row['id']))).mappings().first()
            if receipt is not None:
                return {'archive_id': row['id'], 'generation': receipt['generation'], 'replayed': True}
            settings = tables['settings']
            current = await conn.scalar(select(settings.c.value).where(settings.c.key == GENERATION_KEY))
            if current != row['generation']:
                raise ResetError('A newer operational epoch exists; restoration refused')
            present = _summary(await _snapshot(conn, tables))
            if present['total_deleted_rows'] or present['academy_links_detached']:
                raise ResetError('New operational work exists; restoration refused')
            document = json.loads(row['payload'])
            if document.get('version') != 1:
                raise ResetError('Unsupported operational archive version')
            # Restore parents first using the reflected dependency ordering.
            deleted = document['deleted']
            metadata = tables['bookings'].metadata
            for table in metadata.sorted_tables:
                block = deleted.get(table.name)
                if block is None:
                    continue
                if not set(block['columns']).issubset(table.columns.keys()):
                    raise ResetError(f'Archive schema incompatible with {table.name}')
                for archived in _unpack(block):
                    if await conn.scalar(select(func.count()).select_from(table).where(_ident(table, archived))):
                        raise ResetError(f'New {table.name} record conflicts with archive')
                    await conn.execute(table.insert().values(**archived))
            academy = tables['academy_reviews']
            for archived in _unpack(document['detached_academy_reviews']):
                current_row = (await conn.execute(select(academy).where(_ident(academy, archived)))).mappings().first()
                expected = dict(archived, photo_id=None)
                if current_row is None or dict(current_row) != expected:
                    raise ResetError('An Academy review changed; restoration refused')
                await conn.execute(academy.update().where(_ident(academy, archived)).values(photo_id=archived['photo_id']))
            verified = await _snapshot(conn, tables)
            # A reviewed dependency added after an old archive may be empty.
            # Keep comparing every archived block and any newly present row.
            if (not _restored_blocks_match(verified['deleted'], document['deleted'])
                    or verified['detached_academy_reviews'] != document['detached_academy_reviews']):
                raise ResetError('Restored values do not match the verified archive; transaction rolled back')
            generation, now = str(uuid4()), datetime.now(UTC).replace(tzinfo=None)
            # A fresh generation blocks packets queued during the cleared epoch;
            # prior reset_at is restored for the archived attendance history.
            previous = {item['key']: item['value'] for item in _unpack(document['previous_epoch'])}
            await _set_epoch(conn, settings, generation, previous.get(RESET_AT_KEY, '1970-01-01T00:00:00+00:00'))
            await conn.execute(restored.insert().values(archive_id=row['id'], owner_id=owner_id,
                                                       created_at=now, generation=generation))
            await conn.execute(tables['audit_logs'].insert().values(user_id=owner_id,
                action='operations.restore', entity=ARCHIVE_TABLE, entity_id=row['id'],
                details=_canonical({'request_id': request_id, 'archive_sha256': row['payload_sha256'],
                                    'generation': generation}), created_at=now))
            return {'archive_id': row['id'], 'generation': generation, 'replayed': False}
    finally:
        actor_id.reset(token)
