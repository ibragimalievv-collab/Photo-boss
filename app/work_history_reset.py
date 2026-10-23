"""One owner-requested archived reset of all existing bookings, shoots and sales.

Employees, attendance, lunch, prices, cash payouts and physical files survive.
"""
import json
import logging
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text

from .operational_reset import (
    ARCHIVE_TABLE,
    ResetError,
    _canonical,
    _digest,
    _ident,
    _lock,
    _pack,
    _reflect,
    _rows,
    _verified_archive,
    install_archive_guards,
)
from .operations_guard import current_generation, sqlite_operation_barrier
from .services.booking_notices import notice_booking_id

REQUEST_ID = 'all-booking-shoot-sales-owner-20261021'
REQUEST_ENV = 'PHOTO_BOSS_WORK_HISTORY_RESET_REQUEST'
SCOPE = 'all-existing-booking-shoot-sale-history'
PRODUCTION_SERVICE = 'srv-damohb1mirbc73as0rl0'
log = logging.getLogger(__name__)


def business_rows(before, tables):
    selected = {name: [] for name in tables}
    for name in ('bookings', 'sales', 'sale_drafts'):
        selected[name] = list(before[name])
    # Capture dependents explicitly, including rows usually removed by CASCADE.
    # Learning survives; only its nullable original-photo link is detached.
    changed = True
    while changed:
        changed = False
        for name, table in tables.items():
            if name in {ARCHIVE_TABLE, 'audit_logs', 'academy_reviews'}:
                continue
            for foreign in table.foreign_keys:
                values = {r[foreign.column.name] for r in selected[foreign.column.table.name]}
                added = [r for r in before[name] if r[foreign.parent.name] in values and r not in selected[name]]
                if added:
                    selected[name].extend(added)
                    changed = True
    bids = {r['id'] for r in selected['bookings']}
    sids = {r['id'] for r in selected['sales']}
    pids = {r['manager_payroll_entry_id'] for r in selected['sales']}
    for row in before['payroll_entries']:
        links = dict(re.findall(r'(sale|booking)=(\d+)(?=;|\s|$)', row['note'] or ''))
        if row['id'] in pids or (row['kind'] == 'Комиссия менеджера' and (
                int(links.get('sale', -1)) in sids or int(links.get('booking', -1)) in bids)):
            selected['payroll_entries'].append(row)
    for row in before['notifications']:
        sale = re.fullmatch(r'sale-created:(\d+)', row['event_key'] or '')
        if notice_booking_id(SimpleNamespace(**row)) in bids or (sale and int(sale[1]) in sids):
            selected['notifications'].append(row)
    nids = {r['id'] for r in selected['notifications']}
    selected['push_deliveries'] = [r for r in before['push_deliveries'] if r['notification_id'] in nids]
    prefixes = tuple(f'{prefix}{bid}' for bid in bids for prefix in ('booking:trash:', 'booking-control:advice:'))
    selected['settings'] = [r for r in before['settings'] if prefixes and any(
        r['key'] == prefix or r['key'].startswith(prefix+':') for prefix in prefixes)]
    return {name: rows for name, rows in selected.items() if rows}


async def startup_work_history_reset(engine, raw, admin_ids):
    """A historical service identity alone must never authorize deleting work."""
    if not raw.strip():
        return None
    try:
        request = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ResetError('Invalid work history reset deployment request') from exc
    if (not isinstance(request, dict)
            or set(request) != {'requestId', 'scope', 'expectedGeneration'}
            or request['requestId'] != REQUEST_ID or request['scope'] != SCOPE
            or not isinstance(request['expectedGeneration'], str)
            or len(request['expectedGeneration']) > 100):
        raise ResetError('Invalid work history reset deployment scope')
    return await reset_old_work(engine, admin_ids, expected_generation=request['expectedGeneration'])


async def reset_old_work(engine, admin_ids, *, expected_generation=None):
    async with sqlite_operation_barrier(engine), engine.begin() as conn:
        tables = await _reflect(conn)
        await _lock(conn, tables)
        previous = await _verified_archive(conn, tables[ARCHIVE_TABLE], REQUEST_ID)
        if previous:
            if json.loads(previous['payload']).get('scope') != 'all-existing-booking-shoot-sale-history':
                raise ResetError('Work history reset request scope mismatch')
            result = json.loads(previous['row_counts']) | {'replayed': True}
        else:
            if expected_generation is not None and await current_generation(conn) != expected_generation:
                raise ResetError('Work history reset targets a different operational generation')
            owners = (await conn.execute(text("""SELECT DISTINCT u.id,u.tg_id FROM users u
                JOIN user_roles r ON r.user_id=u.id
                WHERE u.active=true AND r.role='OWNER' ORDER BY u.id"""))).mappings()
            owner = next((r['id'] for r in owners if r['tg_id'] in admin_ids), None)
            if owner is None:
                raise ResetError('Configured active owner required for work history reset')
            before = {name: await _rows(conn, table) for name, table in tables.items()
                      if name not in {ARCHIVE_TABLE, 'audit_logs'}}
            source = before | {ARCHIVE_TABLE: [], 'audit_logs': []}
            deleted = business_rows(source, tables)
            photo_ids = {r['id'] for r in deleted.get('photos', [])}
            detached = [r for r in before['academy_reviews'] if r['photo_id'] in photo_ids]
            generation = str(uuid4())
            epoch = {'operations:generation': generation}
            document = {'version': 4, 'scope': 'all-existing-booking-shoot-sale-history', 'request_id': REQUEST_ID,
                        'scopeBoundary': 'all-existing-at-transaction-lock',
                        'deleted': {n: _pack(tables[n], rows) for n, rows in deleted.items()},
                        'detachedAcademy': _pack(tables['academy_reviews'], detached),
                        'previousEpoch': _pack(tables['settings'], [r for r in before['settings'] if r['key'] in epoch])}
            payload = _canonical(document)
            counts = {name: len(rows) for name, rows in deleted.items()}
            await install_archive_guards(conn)
            await conn.execute(tables[ARCHIVE_TABLE].insert().values(
                request_id=REQUEST_ID, owner_id=owner, created_at=datetime.now(UTC).replace(tzinfo=None),
                generation=generation, payload=payload, payload_sha256=_digest(payload), row_counts=_canonical(counts)))
            saved = await _verified_archive(conn, tables[ARCHIVE_TABLE], REQUEST_ID)
            if not saved or saved['payload'] != payload:
                raise ResetError('Work history archive verification failed')
            if photo_ids:
                academy = tables['academy_reviews']
                await conn.execute(academy.update().where(academy.c.photo_id.in_(photo_ids)).values(photo_id=None))
            # Reverse FK order: delete children before parents, without resetting IDs.
            ordered = next(iter(tables.values())).metadata.sorted_tables
            for table in reversed(ordered):
                for row in deleted.get(table.name, []):
                    await conn.execute(table.delete().where(_ident(table, row)))
            for key, value in epoch.items():
                await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value'), {'key': key, 'value': value})
            for name, rows in before.items():
                expected = [r for r in rows if r not in deleted.get(name, [])]
                if name == 'academy_reviews':
                    expected = [dict(r, photo_id=None) if r in detached else r for r in expected]
                if name == 'settings':
                    expected = [r for r in expected if r['key'] not in epoch]
                    expected += [{'key': key, 'value': value} for key, value in epoch.items()]
                actual = await _rows(conn, tables[name])
                normalize = lambda values, table=tables[name]: sorted(_canonical(_pack(table, [r])) for r in values)
                if normalize(actual) != normalize(expected):
                    raise ResetError(f'Work history preservation failed: {name}; rolled back')
            await conn.execute(tables['audit_logs'].insert().values(
                user_id=owner, action='work_history.owner_reset', entity=ARCHIVE_TABLE,
                entity_id=saved['id'], details=_canonical({'requestId': REQUEST_ID, 'counts': counts,
                    'scopeBoundary': document['scopeBoundary'], 'archiveSha256': saved['payload_sha256']}),
                created_at=datetime.now(UTC).replace(tzinfo=None)))
            result = counts | {'replayed': False}
    log.info('Owner work history reset verified: %s', json.dumps(result, sort_keys=True))
    return result


async def log_booking_readiness(engine):
    """Read-only production configuration evidence, without guest information."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from .models import Hotel, Package
    from .services.pricing import booking_unit_quote

    async with AsyncSession(engine) as session:
        hotels = list(await session.scalars(select(Hotel).where(Hotel.active.is_(True))))
        packages = list(await session.scalars(select(Package).where(Package.active.is_(True))))
        ready = []
        blocked = []
        for hotel in hotels:
            valid = []
            for package in packages:
                try:
                    await booking_unit_quote(session, hotel.id, package.id)
                    valid.append(package.id)
                except ValueError:
                    continue
            (ready if valid else blocked).append(hotel.id)
    result = {'readyHotelIds': ready, 'blockedHotelIds': blocked, 'activePackages': len(packages)}
    log.info('New booking configuration verified: %s', json.dumps(result, sort_keys=True))
    return result
