-- Photo Boss 3.63: database changes for the standalone server (photo-boss.ru).
--
-- The standalone server never changes its schema at startup. It refuses to
-- start while a model column is missing (reviewed_standalone_schema_preparation_required),
-- so apply this file once, as the database owner or postgres, BEFORE starting 3.63:
--
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -d photoboss_production -f 3.63-standalone-schema.sql
--
-- One transaction: either every change is applied or none. Safe to repeat;
-- each step checks what already exists. The 3.62 code keeps working on the
-- changed schema, so returning to 3.62 needs no schema rollback.

BEGIN;
SET LOCAL lock_timeout = '5s';

-- Required: 3.63 does not start without these columns.
-- Camera file names and checksums of uploaded originals (faster shoot upload).
ALTER TABLE photos ADD COLUMN IF NOT EXISTS original_name VARCHAR(255);
ALTER TABLE photos ADD COLUMN IF NOT EXISTS source_sha256 VARCHAR(64);
-- "Гость не купил" outcome of a completed shoot.
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_sale_reason VARCHAR(30);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_sale_note TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_sale_at TIMESTAMP;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_sale_by_id INTEGER REFERENCES users(id);

-- Money stored exactly to the kopeck (earlier releases used double precision).
-- Values were written as roubles and kopecks, so round(..., 2) keeps them exact.
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN SELECT * FROM (VALUES
            ('packages', 'price_per_photo'), ('bookings', 'deposit'),
            ('sales', 'amount'), ('sales', 'commission'),
            ('compensation', 'base_salary'), ('payroll_entries', 'amount'),
            ('shift_check_ins', 'fine_amount'), ('sales_plans', 'target_amount')
        ) AS money(table_name, column_name)
    LOOP
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema()
                     AND table_name = item.table_name AND column_name = item.column_name
                     AND data_type IN ('double precision', 'real')) THEN
            EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE NUMERIC(14,2) USING round(%I::numeric, 2)',
                           item.table_name, item.column_name, item.column_name);
        END IF;
    END LOOP;
END $$;

-- Indexes for daily lists, finance periods and history screens (app/models.py).
CREATE INDEX IF NOT EXISTS ix_photos_source_sha256 ON photos (source_sha256);
CREATE INDEX IF NOT EXISTS ix_photos_shooting_id ON photos (shooting_id);
CREATE INDEX IF NOT EXISTS ix_bookings_shoot_date_status ON bookings (shoot_date, status);
CREATE INDEX IF NOT EXISTS ix_bookings_hotel_id_shoot_date ON bookings (hotel_id, shoot_date);
CREATE INDEX IF NOT EXISTS ix_bookings_photographer_id_shoot_date ON bookings (photographer_id, shoot_date);
CREATE INDEX IF NOT EXISTS ix_bookings_manager_id_shoot_date ON bookings (manager_id, shoot_date);
CREATE INDEX IF NOT EXISTS ix_sales_booking_id ON sales (booking_id);
CREATE INDEX IF NOT EXISTS ix_sales_credited_user_id_created_at ON sales (credited_user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_sales_created_at ON sales (created_at);
CREATE INDEX IF NOT EXISTS ix_payroll_entries_user_id_created_at ON payroll_entries (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_payroll_entries_created_at ON payroll_entries (created_at);
CREATE INDEX IF NOT EXISTS ix_shifts_user_id_start_at ON shifts (user_id, start_at);
CREATE INDEX IF NOT EXISTS ix_shifts_start_at ON shifts (start_at);
CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at);

COMMIT;
