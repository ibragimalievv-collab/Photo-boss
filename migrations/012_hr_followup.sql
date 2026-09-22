-- Additive upgrade; production startup uses app/schema_updates.py.
-- Keep these columns on application rollback: removing them would destroy HR follow-up data.
ALTER TABLE hr_candidates ADD COLUMN IF NOT EXISTS responsible_id INTEGER REFERENCES users(id);
ALTER TABLE hr_candidates ADD COLUMN IF NOT EXISTS region VARCHAR(50) NOT NULL DEFAULT '';
ALTER TABLE hr_candidates ADD COLUMN IF NOT EXISTS reminder_at TIMESTAMP;
