CREATE TABLE IF NOT EXISTS academy_reminders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reminder_date DATE NOT NULL,
    kind VARCHAR(30) NOT NULL DEFAULT 'DAILY_LESSON',
    sent_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_academy_reminder UNIQUE (user_id, reminder_date, kind)
);

CREATE INDEX IF NOT EXISTS ix_academy_reminders_user_id ON academy_reminders(user_id);
CREATE INDEX IF NOT EXISTS ix_academy_reminders_date ON academy_reminders(reminder_date);

CREATE TABLE IF NOT EXISTS academy_certificates (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    certificate_no VARCHAR(40) NOT NULL UNIQUE,
    verification_code VARCHAR(64) NOT NULL UNIQUE,
    final_score INTEGER NOT NULL,
    issued_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT ck_academy_certificate_score CHECK (final_score BETWEEN 85 AND 100)
);

CREATE TABLE IF NOT EXISTS academy_locations (
    id SERIAL PRIMARY KEY,
    hotel_id INTEGER REFERENCES hotels(id) ON DELETE SET NULL,
    name VARCHAR(150) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    shot_plan TEXT NOT NULL DEFAULT '[]',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_academy_locations_hotel_id ON academy_locations(hotel_id);
