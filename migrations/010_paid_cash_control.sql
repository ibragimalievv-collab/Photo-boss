-- Paid outflows are separate from salary accruals. Existing rows are preserved.
ALTER TABLE sales ADD COLUMN IF NOT EXISTS manager_percent_applied VARCHAR(60);
ALTER TABLE sales ADD COLUMN IF NOT EXISTS manager_payroll_entry_id INTEGER REFERENCES payroll_entries(id) ON DELETE SET NULL;
CREATE TABLE IF NOT EXISTS cash_movements (
	id SERIAL NOT NULL,
	category VARCHAR(20) NOT NULL,
	amount NUMERIC(14, 2) NOT NULL,
	paid_on DATE NOT NULL,
	hotel_id INTEGER,
	employee_id INTEGER,
	note TEXT NOT NULL,
	status VARCHAR(20) NOT NULL,
	created_by_id INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	voided_by_id INTEGER,
	voided_at TIMESTAMP WITHOUT TIME ZONE,
	void_reason TEXT,
	PRIMARY KEY (id),
	CHECK (category IN ('HOTEL','TAX','PAYROLL','OTHER')),
	CHECK (amount>0),
	CHECK (status IN ('POSTED','VOIDED')),
	CHECK ((category='PAYROLL' AND employee_id IS NOT NULL) OR (category<>'PAYROLL' AND employee_id IS NULL)),
	FOREIGN KEY(hotel_id) REFERENCES hotels (id),
	FOREIGN KEY(employee_id) REFERENCES users (id),
	FOREIGN KEY(created_by_id) REFERENCES users (id),
	FOREIGN KEY(voided_by_id) REFERENCES users (id)
);
CREATE INDEX IF NOT EXISTS ix_cash_movements_paid_on ON cash_movements (paid_on);
CREATE OR REPLACE FUNCTION pb_capture_change() RETURNS trigger AS $$
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
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS pb_audit_cash_movements ON cash_movements;
CREATE TRIGGER pb_audit_cash_movements AFTER INSERT OR UPDATE OR DELETE ON cash_movements FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_settings ON settings;
CREATE TRIGGER pb_audit_settings AFTER INSERT OR UPDATE OR DELETE ON settings FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
