"""Trusted, one-shot deployment request; no remotely callable reset endpoint."""
import json
import logging
import re

from sqlalchemy import text

from .operational_reset import ResetError, inspect_archive, preview, reset
from .operations_guard import (
    ADVISORY_KEY,
    MAINTENANCE_KEY,
    current_generation,
    sqlite_operation_barrier,
)

logger = logging.getLogger(__name__)
REQUEST_ENV = 'PHOTO_BOSS_OPERATIONS_RESET_REQUEST'


def parse_request(raw):
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ResetError('Invalid operations reset deployment request') from exc
    if (not isinstance(value, dict) or set(value) != {'requestId', 'expectedGeneration', 'scope'}
            or value['scope'] != 'all-operational-history'
            or not isinstance(value['requestId'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', value['requestId'])
            or not isinstance(value['expectedGeneration'], str)
            or len(value['expectedGeneration']) > 100):
        raise ResetError('Invalid operations reset deployment request')
    return value


async def startup_reset(engine, raw, admin_ids):
    request = parse_request(raw)
    if request is None:
        summary = await preview(engine)
        logger.info('Operations reset preview: %s', json.dumps(summary, sort_keys=True))
        return None
    request_id = request['requestId']
    async with sqlite_operation_barrier(engine), engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            await conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': ADVISORY_KEY})
        existing = (await conn.execute(text('SELECT id FROM operational_reset_archives WHERE request_id=:request'),
                                       {'request': request_id})).scalar()
        if existing is not None:
            # A retained env value, restart or retry never deletes fresh work.
            completed = True
        else:
            completed = False
            if await current_generation(conn) != request['expectedGeneration']:
                raise ResetError('Reset deployment targets a different operational generation')
            owner_ids = list((await conn.execute(text("""SELECT DISTINCT u.id,u.tg_id
                FROM users u JOIN user_roles r ON r.user_id=u.id
                WHERE u.active=true AND r.role='OWNER' ORDER BY u.id"""))).mappings())
            owner_id = next((row['id'] for row in owner_ids if row['tg_id'] in admin_ids), None)
            if owner_id is None:
                raise ResetError('No configured active owner authorizes the reset')
            maintenance = (await conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                                              {'key': MAINTENANCE_KEY})).scalar()
            if maintenance and maintenance != request_id:
                raise ResetError('Another operational maintenance request is active')
            await conn.execute(text('''INSERT INTO settings(key,value) VALUES (:key,:value)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value'''),
                {'key': MAINTENANCE_KEY, 'value': request_id})
    if completed:
        result = await inspect_archive(engine, request_id)
        result['replayed'] = True
    else:
        # Maintenance is committed and all existing shared leases have drained.
        # It remains set if any verification fails, so no partial reset is used.
        summary = await preview(engine)
        logger.info('Operations reset frozen preview: %s', json.dumps(summary, sort_keys=True))
        result = await reset(engine, request_id, owner_id,
                             expected_preview_sha256=summary['snapshot_sha256'],
                             expected_generation=request['expectedGeneration'])
        verified = await inspect_archive(engine, request_id)
        if verified['archive_sha256'] != result['archive_sha256']:
            raise ResetError('Persisted reset archive failed independent verification')
    logger.info('Operations reset verified: %s', json.dumps(result, sort_keys=True))
    return result
