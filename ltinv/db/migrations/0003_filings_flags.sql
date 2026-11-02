-- 0003: filings carry their source link and a dedupe key; flags record what matched,
-- the date for data rules (no filing), and the thesis kill criterion they hit.
ALTER TABLE filings ADD COLUMN company TEXT;
ALTER TABLE filings ADD COLUMN url TEXT;
ALTER TABLE filings ADD COLUMN uid TEXT;
ALTER TABLE filings ADD COLUMN text_path TEXT;
CREATE UNIQUE INDEX ux_filings_uid ON filings(exchange, uid);

ALTER TABLE flags ADD COLUMN trigger_date TEXT;
ALTER TABLE flags ADD COLUMN detail TEXT;
ALTER TABLE flags ADD COLUMN kill_criterion TEXT;
CREATE UNIQUE INDEX ux_flags_once ON flags(symbol, rule_id, COALESCE(filing_id, -1), COALESCE(trigger_date, ''));
