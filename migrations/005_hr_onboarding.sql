-- Additive; init_db also creates these missing tables.

CREATE TABLE IF NOT EXISTS hr_candidates (
	id SERIAL NOT NULL, 
	name VARCHAR(150) NOT NULL, 
	contact VARCHAR(300) NOT NULL, 
	source VARCHAR(150) NOT NULL, 
	role VARCHAR(30) NOT NULL, 
	stage VARCHAR(30) NOT NULL, 
	interview_at TIMESTAMP WITHOUT TIME ZONE, 
	decision TEXT NOT NULL, 
	notes TEXT NOT NULL, 
	employee_id INTEGER, 
	created_by_id INTEGER NOT NULL, 
	revision INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (stage IN ('NEW','CONTACTED','INTERVIEW','OFFER','DOCUMENTS','HIRED','REJECTED')), 
	CHECK (role IN ('PHOTOGRAPHER','MANAGER')), 
	UNIQUE (employee_id), 
	FOREIGN KEY(employee_id) REFERENCES users (id), 
	FOREIGN KEY(created_by_id) REFERENCES users (id)
)

;

CREATE TABLE IF NOT EXISTS academy_assessments (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	kind VARCHAR(40) NOT NULL, 
	score INTEGER NOT NULL, 
	result TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (score BETWEEN 0 AND 100), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;
CREATE INDEX IF NOT EXISTS ix_academy_assessments_user_id ON academy_assessments (user_id);
