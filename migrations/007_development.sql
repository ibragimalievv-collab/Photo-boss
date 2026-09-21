-- Additive, also applied by init_db. No historic rows rewritten.

CREATE TABLE IF NOT EXISTS shoot_development_reviews (
	id SERIAL NOT NULL, 
	shooting_id INTEGER NOT NULL, 
	photographer_id INTEGER NOT NULL, 
	fingerprint VARCHAR(64) NOT NULL, 
	photo_ids TEXT NOT NULL, 
	analyzed TEXT NOT NULL, 
	result TEXT, 
	status VARCHAR(30) NOT NULL, 
	attempts INTEGER NOT NULL, 
	last_error TEXT, 
	claimed_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_shoot_review_fingerprint UNIQUE (shooting_id, fingerprint), 
	FOREIGN KEY(shooting_id) REFERENCES shootings (id), 
	FOREIGN KEY(photographer_id) REFERENCES users (id)
)

;
CREATE INDEX IF NOT EXISTS ix_shoot_development_reviews_photographer_id ON shoot_development_reviews (photographer_id);
CREATE INDEX IF NOT EXISTS ix_shoot_development_reviews_status ON shoot_development_reviews (status);
CREATE INDEX IF NOT EXISTS ix_shoot_development_reviews_shooting_id ON shoot_development_reviews (shooting_id);

CREATE TABLE IF NOT EXISTS sales_training_sessions (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	client_type VARCHAR(60) NOT NULL, 
	transcript TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	revision INTEGER NOT NULL, 
	evaluation TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;
CREATE INDEX IF NOT EXISTS ix_sales_training_sessions_user_id ON sales_training_sessions (user_id);
