-- 0002: asset class per holding (equity|fund|govt|cash_like|other), set by fetch holdings.
ALTER TABLE holdings ADD COLUMN asset_class TEXT;
