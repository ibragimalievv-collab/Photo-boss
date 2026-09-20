ALTER TABLE training_assignments ADD COLUMN IF NOT EXISTS ai_score INTEGER;
ALTER TABLE training_assignments ADD COLUMN IF NOT EXISTS ai_analysis TEXT;
ALTER TABLE training_assignments ADD COLUMN IF NOT EXISTS review_source VARCHAR(20);

ALTER TABLE training_assignments
    DROP CONSTRAINT IF EXISTS ck_training_ai_score;
ALTER TABLE training_assignments
    ADD CONSTRAINT ck_training_ai_score
    CHECK (ai_score IS NULL OR ai_score BETWEEN 0 AND 100);
