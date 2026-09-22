-- Preserve frame history; checkpoint large-shoot summaries between AI requests.
ALTER TABLE shoot_development_reviews ADD COLUMN IF NOT EXISTS summary_parts TEXT NOT NULL DEFAULT '[]';
