-- Also applied idempotently by app.schema_updates.upgrade during init_db.
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS event_key VARCHAR(160);
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS priority VARCHAR(20) NOT NULL DEFAULT 'info';
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS kind VARCHAR(50) NOT NULL DEFAULT 'legacy';
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS payload TEXT;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMP;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP;
CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_event ON notifications(user_id,event_key);
