-- init_db also installs these triggers idempotently.
CREATE OR REPLACE FUNCTION pb_capture_change() RETURNS trigger AS $$
DECLARE before_row jsonb; after_row jsonb; actor integer; row_id integer;
BEGIN
    actor := NULLIF(current_setting('photo_boss.actor', true), '')::integer;
    IF TG_OP <> 'INSERT' THEN before_row := to_jsonb(OLD) - ARRAY['file_id','full_body_file_id','workplace_file_id','receipt_file_id']; END IF;
    IF TG_OP <> 'DELETE' THEN after_row := to_jsonb(NEW) - ARRAY['file_id','full_body_file_id','workplace_file_id','receipt_file_id']; END IF;
    IF TG_OP = 'UPDATE' AND before_row = after_row THEN RETURN NEW; END IF;
    row_id := COALESCE((after_row->>'id')::integer, (before_row->>'id')::integer);
    INSERT INTO audit_logs(user_id,action,entity,entity_id,details,created_at)
      VALUES(actor,'row.' || lower(TG_OP),TG_TABLE_NAME,row_id,
             jsonb_build_object('before',before_row,'after',after_row,'capture','database')::text,
             CURRENT_TIMESTAMP AT TIME ZONE 'UTC');
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS pb_audit_users ON users;
CREATE TRIGGER pb_audit_users AFTER INSERT OR UPDATE OR DELETE ON users FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_user_roles ON user_roles;
CREATE TRIGGER pb_audit_user_roles AFTER INSERT OR UPDATE OR DELETE ON user_roles FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_hotel_employees ON hotel_employees;
CREATE TRIGGER pb_audit_hotel_employees AFTER INSERT OR UPDATE OR DELETE ON hotel_employees FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_bookings ON bookings;
CREATE TRIGGER pb_audit_bookings AFTER INSERT OR UPDATE OR DELETE ON bookings FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_shootings ON shootings;
CREATE TRIGGER pb_audit_shootings AFTER INSERT OR UPDATE OR DELETE ON shootings FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_sales ON sales;
CREATE TRIGGER pb_audit_sales AFTER INSERT OR UPDATE OR DELETE ON sales FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_sale_drafts ON sale_drafts;
CREATE TRIGGER pb_audit_sale_drafts AFTER INSERT OR UPDATE OR DELETE ON sale_drafts FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_receipts ON receipts;
CREATE TRIGGER pb_audit_receipts AFTER INSERT OR UPDATE OR DELETE ON receipts FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_payroll_entries ON payroll_entries;
CREATE TRIGGER pb_audit_payroll_entries AFTER INSERT OR UPDATE OR DELETE ON payroll_entries FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_compensation ON compensation;
CREATE TRIGGER pb_audit_compensation AFTER INSERT OR UPDATE OR DELETE ON compensation FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_shifts ON shifts;
CREATE TRIGGER pb_audit_shifts AFTER INSERT OR UPDATE OR DELETE ON shifts FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_shift_check_ins ON shift_check_ins;
CREATE TRIGGER pb_audit_shift_check_ins AFTER INSERT OR UPDATE OR DELETE ON shift_check_ins FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_shift_check_outs ON shift_check_outs;
CREATE TRIGGER pb_audit_shift_check_outs AFTER INSERT OR UPDATE OR DELETE ON shift_check_outs FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_packages ON packages;
CREATE TRIGGER pb_audit_packages AFTER INSERT OR UPDATE OR DELETE ON packages FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
DROP TRIGGER IF EXISTS pb_audit_bank_reconciliations ON bank_reconciliations;
CREATE TRIGGER pb_audit_bank_reconciliations AFTER INSERT OR UPDATE OR DELETE ON bank_reconciliations FOR EACH ROW EXECUTE FUNCTION pb_capture_change();
