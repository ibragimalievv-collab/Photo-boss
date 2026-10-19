"""Owner-authorized sales/fines reset; attendance, media and other work survive."""
import json
import logging
import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, text

from .operational_reset import (
    ARCHIVE_TABLE,
    ResetError,
    _archive_result,
    _canonical,
    _digest,
    _ident,
    _lock,
    _owner,
    _pack,
    _reflect,
    _rows,
    _verified_archive,
    install_archive_guards,
)
from .operations_guard import MAINTENANCE_KEY, sqlite_operation_barrier

REQUEST_ENV = 'PHOTO_BOSS_SALES_FINES_RESET_REQUEST'
GENERATION_KEY = 'sales:generation'
log = logging.getLogger(__name__)


async def reset_sales_fines(engine, request_id, admin_ids, *, hide_hotel_name=None, clear_bookings=False):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
        raise ResetError('A unique sales reset request ID is required')
    if hide_hotel_name not in (None, 'Основной отель') or type(clear_bookings) is not bool:
        raise ResetError('Unsupported reset scope')
    async with sqlite_operation_barrier(engine), engine.begin() as conn:
        tables = await _reflect(conn)
        await _lock(conn, tables)
        previous = await _verified_archive(conn, tables[ARCHIVE_TABLE], request_id)
        if previous:
            if json.loads(previous['payload']).get('scope') != 'sales-and-fines':
                raise ResetError('Reset request ID belongs to another scope')
            return _archive_result(previous) | {'replayed': True}
        owners = (await conn.execute(text("SELECT DISTINCT u.id,u.tg_id FROM users u JOIN user_roles r ON r.user_id=u.id WHERE u.active=true AND r.role='OWNER' ORDER BY u.id"))).mappings()
        owner = next((row['id'] for row in owners if row['tg_id'] in admin_ids), None)
        if owner is None:
            raise ResetError('An active configured owner is required')
        await _owner(conn, tables, owner)
        if await conn.scalar(select(tables['settings'].c.value).where(tables['settings'].c.key == MAINTENANCE_KEY)):
            raise ResetError('Another reset is in progress')
        # Snapshot every row under the same locks, then verify every unrelated
        # table byte-for-byte. Only audit append operations are excluded.
        before = {name: await _rows(conn, table) for name, table in tables.items()
                  if name not in {ARCHIVE_TABLE, 'audit_logs'}}
        prior_audit = await _rows(conn, tables['audit_logs'])
        audit_max_id = max((row['id'] for row in prior_audit), default=0)
        sales = before['sales']
        drafts = [r for r in before['sale_drafts'] if r['status'] != 'CANCELLED']
        bids = {r['booking_id'] for r in sales + drafts if r['booking_id'] is not None}
        sale_ids = {r['id'] for r in sales}
        payroll_ids = {r['manager_payroll_entry_id'] for r in sales}
        def remove_payroll(row):
            links = dict(re.findall(r'(sale|booking)=(\d+)(?=;|\s|$)', row['note'] or ''))
            return ('штраф' in row['kind'].casefold() or row['id'] in payroll_ids or
                    (row['kind'] == 'Комиссия менеджера' and
                     (int(links.get('sale', -1)) in sale_ids or int(links.get('booking', -1)) in bids)))
        deleted = {'guest_feedback': [r for r in before['guest_feedback'] if r['sale_id'] in sale_ids],
                   'sales': sales, 'payroll_entries': [r for r in before['payroll_entries'] if remove_payroll(r)]}
        now = datetime.now(UTC).replace(tzinfo=None)
        changes = {
            'hotels': [(r, dict(r, active=False)) for r in before['hotels']
                       if hide_hotel_name and r['name'] == hide_hotel_name and r['active']],
            'sale_drafts': [(r, dict(r, status='CANCELLED')) for r in drafts],
            'bookings': ([(r, dict(r, status='CANCELLED', cancelled_at=now,
                                  cancellation_reason='Обнулено владельцем')) for r in before['bookings']]
                         if clear_bookings else [(r, dict(r, status='READY_FOR_SALE')) for r in before['bookings']
                         if r['id'] in bids and r['status'] in {'SOLD', 'COMPLETED'}]),
            # Preserve actual arrival time and the late flag as historical fact.
            'shift_check_ins': [(r, dict(r, fine_amount=type(r['fine_amount'])(0))) for r in before['shift_check_ins'] if r['fine_amount']],
            'receipts': [(r, dict(r, status='REJECTED', reviewed_by_id=owner,
                                 reviewed_at=datetime.now(UTC).replace(tzinfo=None)))
                         for r in before['receipts'] if r['booking_id'] in bids
                         and r['purpose'] == 'PAYMENT' and r['status'] == 'PENDING'],
        }
        generation = str(uuid4())
        old_setting = next((r for r in before['settings'] if r['key'] == GENERATION_KEY), None)
        setting = {'key': GENERATION_KEY, 'value': generation}
        changes['settings'] = [(old_setting, setting)]
        if clear_bookings:
            for booking in before['bookings']:
                key = f'booking:trash:{booking["id"]}'
                old = next((r for r in before['settings'] if r['key'] == key), None)
                changes['settings'].append((old, {'key': key, 'value': _canonical({
                    'by': owner, 'at': now.isoformat()+'Z', 'reason': 'Обнулено владельцем'})}))
        document = {'version': 2, 'scope': 'sales-and-fines', 'request_id': request_id,
                    'deleted': {n: _pack(tables[n], rows) for n, rows in deleted.items()},
                    'changes': {n: {'before': _pack(tables[n], [a for a, _ in pairs if a is not None]),
                                    'after': _pack(tables[n], [b for _, b in pairs])}
                                for n, pairs in changes.items()}}
        payload = _canonical(document)
        counts = {n: len(rows) for n, rows in deleted.items()}
        counts.update(cancelled_drafts=len(drafts), cleared_attendance_fines=len(changes['shift_check_ins']))
        counts.update(hidden_hotels=len(changes['hotels']), cleared_bookings=len(before['bookings']) if clear_bookings else 0)
        await install_archive_guards(conn)
        result = await conn.execute(tables[ARCHIVE_TABLE].insert().values(request_id=request_id,
            owner_id=owner, created_at=now, generation=generation, payload=payload,
            payload_sha256=_digest(payload), row_counts=_canonical(counts)))
        saved = await _verified_archive(conn, tables[ARCHIVE_TABLE], request_id)
        if not saved or saved['payload'] != payload:
            raise ResetError('Sales reset archive verification failed')
        expected = {n: list(rows) for n, rows in before.items()}
        for name, rows in deleted.items():
            table = tables[name]
            for row in rows:
                await conn.execute(table.delete().where(_ident(table, row)))
            expected[name] = [r for r in before[name] if r not in rows]
        for name, pairs in changes.items():
            table = tables[name]
            for old, new in pairs:
                if old is None:
                    await conn.execute(table.insert().values(**new))
                    expected[name].append(new)
                else:
                    await conn.execute(table.update().where(_ident(table, old)).values(**new))
                    expected[name] = [new if r == old else r for r in expected[name]]
        for name, rows in expected.items():
            actual = await _rows(conn, tables[name])
            normalize = lambda values, table=tables[name]: sorted(_canonical(_pack(table, [r])) for r in values)
            if normalize(actual) != normalize(rows):
                raise ResetError(f'Sales reset preservation failed for {name}; rolled back')
        if await _rows(conn, tables['audit_logs'], tables['audit_logs'].c.id <= audit_max_id) != prior_audit:
            raise ResetError('Existing audit history changed; reset rolled back')
        await conn.execute(tables['audit_logs'].insert().values(user_id=owner,
            action='sales_fines.reset', entity=ARCHIVE_TABLE, entity_id=result.inserted_primary_key[0],
            details=_canonical({'request_id': request_id, 'row_counts': counts,
                                'archive_sha256': saved['payload_sha256']}), created_at=now))
        return _archive_result(saved) | {'replayed': False}


async def startup_sales_reset(engine, raw, admin_ids):
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ResetError('Invalid sales reset deployment request') from exc
    if (not isinstance(value, dict) or not {'requestId', 'scope'} <= set(value)
            or set(value) - {'requestId', 'scope', 'hideHotelName', 'clearBookings'}
            or value['scope'] != 'sales-and-fines' or value.get('hideHotelName') not in (None, 'Основной отель')
            or type(value.get('clearBookings', False)) is not bool):
        raise ResetError('Invalid sales reset deployment scope')
    result = await reset_sales_fines(engine, value['requestId'], admin_ids,
        hide_hotel_name=value.get('hideHotelName'), clear_bookings=value.get('clearBookings', False))
    log.info('Sales and fines reset verified: %s', json.dumps(result, sort_keys=True))
    return result
