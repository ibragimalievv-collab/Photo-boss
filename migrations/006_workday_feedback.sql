-- Additive; init_db applies the equivalent schema.
ALTER TABLE shift_check_outs ADD COLUMN IF NOT EXISTS report_note TEXT;
ALTER TABLE shift_check_outs ADD COLUMN IF NOT EXISTS report_saved_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS work_checklist_completions (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	shift_date DATE NOT NULL, 
	item_key VARCHAR(80) NOT NULL, 
	done BOOLEAN NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_checklist_user_day_item UNIQUE (user_id, shift_date, item_key), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;
CREATE INDEX IF NOT EXISTS ix_work_checklist_completions_user_id ON work_checklist_completions (user_id);

CREATE TABLE IF NOT EXISTS operation_requests (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	request_key VARCHAR(80) NOT NULL, 
	payload_hash VARCHAR(64) NOT NULL, 
	result TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_operation_request_user_key UNIQUE (user_id, request_key), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;
CREATE INDEX IF NOT EXISTS ix_operation_requests_user_id ON operation_requests (user_id);

CREATE TABLE IF NOT EXISTS guest_feedback (
	id SERIAL NOT NULL, 
	sale_id INTEGER NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	rating INTEGER, 
	comment TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	submitted_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	CHECK (rating IS NULL OR rating BETWEEN 1 AND 5), 
	UNIQUE (sale_id), 
	FOREIGN KEY(sale_id) REFERENCES sales (id), 
	UNIQUE (token_hash)
)

;
