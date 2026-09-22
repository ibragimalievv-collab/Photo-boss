-- Additive: keep historical prices unknown rather than inventing them.
ALTER TABLE sale_drafts ADD COLUMN IF NOT EXISTS unit_price NUMERIC(14,2);
ALTER TABLE sale_drafts ADD COLUMN IF NOT EXISTS discount_percent NUMERIC(5,2);
ALTER TABLE sales ADD COLUMN IF NOT EXISTS unit_price NUMERIC(14,2);
ALTER TABLE sales ADD COLUMN IF NOT EXISTS discount_percent NUMERIC(5,2);
