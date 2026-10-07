ALTER TABLE photo_storage
    ADD COLUMN IF NOT EXISTS collection VARCHAR(20) NOT NULL DEFAULT 'ALL';

CREATE INDEX IF NOT EXISTS ix_photo_storage_collection
    ON photo_storage (collection);

ALTER TABLE photo_storage
    DROP CONSTRAINT IF EXISTS ck_photo_storage_collection;

ALTER TABLE photo_storage
    ADD CONSTRAINT ck_photo_storage_collection
    CHECK (collection IN ('ALL', 'SELECTED'));
