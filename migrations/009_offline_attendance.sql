-- Keep the original attendance ledger. Device time remains unverified until review.
ALTER TABLE shift_check_ins ADD COLUMN IF NOT EXISTS offline_claimed_at TIMESTAMP;
ALTER TABLE shift_check_outs ADD COLUMN IF NOT EXISTS offline_claimed_at TIMESTAMP;
