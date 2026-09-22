"""Append row snapshots to the existing audit log in the same transaction.

Database triggers cover ORM and raw SQL, including operations outside UI handlers.
The account running these triggers is the application account, not an immutable
external audit service; PostgreSQL administrators can still change database data.
"""
from sqlalchemy import inspect

TABLES = ('users', 'user_roles', 'hotel_employees', 'bookings', 'shootings', 'sales',
          'sale_drafts', 'receipts', 'payroll_entries', 'compensation', 'shifts',
          'shift_check_ins', 'shift_check_outs', 'packages', 'bank_reconciliations', 'cash_movements', 'settings')
# Opaque media identifiers are not needed to inspect changes to financial records.
OMIT = {'file_id', 'full_body_file_id', 'workplace_file_id', 'receipt_file_id'}

POSTGRES_FUNCTION = '''CREATE OR REPLACE FUNCTION pb_capture_change() RETURNS trigger AS $$
DECLARE before_row jsonb; after_row jsonb; actor integer; row_id integer;
BEGIN
    actor := NULLIF(current_setting('photo_boss.actor', true), '')::integer;
    IF TG_OP <> 'INSERT' THEN before_row := to_jsonb(OLD) - ARRAY['file_id','full_body_file_id','workplace_file_id','receipt_file_id']; END IF;
    IF TG_OP <> 'DELETE' THEN after_row := to_jsonb(NEW) - ARRAY['file_id','full_body_file_id','workplace_file_id','receipt_file_id']; END IF;
    IF TG_TABLE_NAME = 'settings' THEN
        IF COALESCE(before_row->>'key','') NOT IN ('PHOTO_PRICE','MANAGER_PERCENT','PHOTOGRAPHER_PERCENT') THEN before_row := NULL; END IF;
        IF COALESCE(after_row->>'key','') NOT IN ('PHOTO_PRICE','MANAGER_PERCENT','PHOTOGRAPHER_PERCENT') THEN after_row := NULL; END IF;
        IF before_row IS NULL AND after_row IS NULL THEN
            IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND before_row = after_row THEN RETURN NEW; END IF;
    row_id := COALESCE((after_row->>'id')::integer, (before_row->>'id')::integer);
    INSERT INTO audit_logs(user_id,action,entity,entity_id,details,created_at)
      VALUES(actor,'row.' || lower(TG_OP),TG_TABLE_NAME,row_id,
             jsonb_build_object('before',before_row,'after',after_row,'capture','database')::text,
             CURRENT_TIMESTAMP AT TIME ZONE 'UTC');
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END; $$ LANGUAGE plpgsql;'''


async def install_capture(conn):
    existing=await conn.run_sync(lambda c:set(inspect(c).get_table_names()))
    tables=[t for t in TABLES if t in existing]
    if conn.dialect.name=='postgresql':
        await conn.exec_driver_sql(POSTGRES_FUNCTION)
        for table in tables:
            await conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS pb_audit_{table} ON {table}')
            await conn.exec_driver_sql(f'CREATE TRIGGER pb_audit_{table} AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION pb_capture_change()')
    elif conn.dialect.name=='sqlite':
        for table in tables:
            columns=await conn.run_sync(lambda c,t=table:[r['name'] for r in inspect(c).get_columns(t) if r['name'] not in OMIT])
            def snapshot(prefix, columns=columns):
                return 'json_object('+','.join(f"'{c}',{prefix}.\"{c}\"" for c in columns)+')'
            for op in ('INSERT','UPDATE','DELETE'):
                before=snapshot('OLD') if op!='INSERT' else 'NULL'
                after=snapshot('NEW') if op!='DELETE' else 'NULL'
                if table=='settings':
                    allowed="('PHOTO_PRICE','MANAGER_PERCENT','PHOTOGRAPHER_PERCENT')"
                    if op!='INSERT': before=f'CASE WHEN OLD.key IN {allowed} THEN {before} ELSE NULL END'
                    if op!='DELETE': after=f'CASE WHEN NEW.key IN {allowed} THEN {after} ELSE NULL END'
                condition=f'WHEN {before} IS NOT {after}' if op=='UPDATE' or table=='settings' else ''
                row='OLD' if op=='DELETE' else 'NEW'
                eid='NULL' if table=='settings' else f'{row}.id'
                # The function is registered on the connection before init_db runs.
                # Refresh column lists after additive schema upgrades as well.
                await conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS pb_audit_{table}_{op.lower()}')
                await conn.exec_driver_sql(f'''CREATE TRIGGER pb_audit_{table}_{op.lower()}
                    AFTER {op} ON {table} {condition} BEGIN
                    INSERT INTO audit_logs(user_id,action,entity,entity_id,details,created_at)
                    VALUES(pb_actor_id(),'row.{op.lower()}','{table}',{eid},
                    json_object('before',{before},'after',{after},'capture','database'),CURRENT_TIMESTAMP); END''')
