-- 0004: every delivery trade from any source (Console tradebook import, Kite trades API),
-- the basis for tax lots (FIFO) and reconciliation; Kite cash snapshots for the risk check.
CREATE TABLE trades (
  id INTEGER PRIMARY KEY,
  trade_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  isin TEXT,
  trade_date TEXT NOT NULL,          -- YYYY-MM-DD (IST)
  side TEXT NOT NULL,                -- BUY|SELL
  qty INTEGER NOT NULL,
  price REAL NOT NULL,
  exchange TEXT,
  product TEXT,                      -- CNC|MIS|NULL (tradebook has none)
  source TEXT NOT NULL,              -- tradebook|kite
  imported_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_trades ON trades(symbol, trade_id, side);
CREATE INDEX idx_trades_symbol ON trades(symbol, trade_date);

CREATE TABLE account_snapshots (
  as_of TEXT PRIMARY KEY,
  cash REAL NOT NULL,                -- Kite equity margin available, INR
  raw_json TEXT
);

ALTER TABLE approvals ADD COLUMN approved_by TEXT;
ALTER TABLE approvals ADD COLUMN note TEXT;
ALTER TABLE approvals ADD COLUMN held_qty INTEGER;   -- holding when approved, for the holdings fallback
