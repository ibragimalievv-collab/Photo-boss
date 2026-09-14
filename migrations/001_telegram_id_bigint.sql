-- Apply only to an existing PostgreSQL database, after a backup.
-- A fresh database gets BIGINT directly from SQLAlchemy metadata.
BEGIN;
ALTER TABLE users ALTER COLUMN tg_id TYPE BIGINT;
COMMIT;
