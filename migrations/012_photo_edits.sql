-- Additive: existing originals and storage queue are unchanged.

CREATE TABLE IF NOT EXISTS photo_edits (
	id SERIAL NOT NULL,
	photo_id INTEGER NOT NULL,
	created_by_id INTEGER NOT NULL,
	mode VARCHAR(10) NOT NULL,
	status VARCHAR(20) NOT NULL,
	parameters TEXT,
	disk_path VARCHAR(500),
	last_error TEXT,
	attempts INTEGER NOT NULL,
	claimed_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CHECK (mode IN ('MANUAL','AI')),
	CHECK (status IN ('PENDING','RUNNING','READY','FAILED','CANCELLED')),
	FOREIGN KEY(photo_id) REFERENCES photos (id) ON DELETE CASCADE,
	FOREIGN KEY(created_by_id) REFERENCES users (id)
)

;
CREATE INDEX IF NOT EXISTS ix_photo_edits_photo_id ON photo_edits (photo_id);
CREATE INDEX IF NOT EXISTS ix_photo_edits_status ON photo_edits (status);
