-- 0001: core tables (Architecture §4). Times are ISO-8601 strings with the IST offset.

CREATE TABLE holdings (
  symbol          TEXT PRIMARY KEY,
  exchange        TEXT,
  isin            TEXT,
  qty             INTEGER NOT NULL,
  avg_price       REAL,
  first_buy_date  TEXT,
  sector          TEXT,
  as_of           TEXT NOT NULL
);

CREATE TABLE lots (
  id         INTEGER PRIMARY KEY,
  symbol     TEXT NOT NULL,
  qty        INTEGER NOT NULL,
  buy_price  REAL NOT NULL,
  buy_date   TEXT NOT NULL,
  source     TEXT NOT NULL
);

CREATE TABLE prices (
  symbol  TEXT NOT NULL,
  date    TEXT NOT NULL,
  o REAL, h REAL, l REAL, c REAL NOT NULL,
  volume  INTEGER,
  source  TEXT NOT NULL,          -- bhavcopy|kite
  PRIMARY KEY (symbol, date)
);

CREATE TABLE financials (
  symbol TEXT NOT NULL, period TEXT NOT NULL,
  revenue REAL, ebitda REAL, pat REAL, ocf REAL, capex REAL, net_debt REAL,
  equity REAL, receivable_days REAL, inventory_days REAL,
  source TEXT,                    -- xbrl|screener
  PRIMARY KEY (symbol, period)
);

CREATE TABLE metrics (
  symbol TEXT NOT NULL, period TEXT NOT NULL,
  roce REAL, ebitda_margin REAL, ocf_pat REAL, nd_ebitda REAL,
  rev_growth_yoy REAL, pat_growth_yoy REAL,
  PRIMARY KEY (symbol, period)
);

CREATE TABLE shareholding (
  symbol TEXT NOT NULL, quarter TEXT NOT NULL,
  promoter_pct REAL, pledge_pct REAL, fii_pct REAL, dii_pct REAL, public_pct REAL,
  PRIMARY KEY (symbol, quarter)
);

CREATE TABLE filings (
  id INTEGER PRIMARY KEY,
  symbol TEXT NOT NULL, exchange TEXT, filed_at TEXT NOT NULL,
  category TEXT, subject TEXT, pdf_path TEXT, raw_json TEXT,
  seen_by_rules INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE flags (
  id INTEGER PRIMARY KEY,
  symbol TEXT NOT NULL, filing_id INTEGER REFERENCES filings(id),
  rule_id TEXT NOT NULL, severity TEXT NOT NULL, fired_at TEXT NOT NULL,
  llm_confirmed INTEGER, llm_rationale TEXT,
  status TEXT NOT NULL DEFAULT 'open'          -- open|resolved|dismissed
);

CREATE TABLE valuations (
  symbol TEXT NOT NULL, as_of TEXT NOT NULL,
  fv_low REAL, fv_base REAL, fv_high REAL, method TEXT, inputs_json TEXT,
  PRIMARY KEY (symbol, as_of)
);

CREATE TABLE theses (
  symbol TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  target_weight_pct REAL,
  kill_criteria_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL
);

CREATE TABLE reviews (
  id INTEGER PRIMARY KEY,
  symbol TEXT NOT NULL, kind TEXT NOT NULL,    -- quarterly|health
  period TEXT NOT NULL, path TEXT NOT NULL,
  verdict TEXT,                                -- intact|weakening|broken|n/a
  created_at TEXT NOT NULL
);

CREATE TABLE risk_snapshots (as_of TEXT PRIMARY KEY, json TEXT NOT NULL);

CREATE TABLE proposals (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
  limit_low REAL, limit_high REAL, reason TEXT NOT NULL,
  evidence_json TEXT, tax_json TEXT, risk_json TEXT,
  status TEXT NOT NULL DEFAULT 'open'  -- open|approved|reconciled|expired|rejected|refused
);

CREATE TABLE approvals (
  proposal_id INTEGER PRIMARY KEY REFERENCES proposals(id),
  approved_at TEXT NOT NULL, valid_until TEXT NOT NULL, channel TEXT NOT NULL,
  qty_override INTEGER, price_override REAL, risk_override_note TEXT,
  token_hash TEXT                      -- Phase 6 execute only
);

CREATE TABLE fills (
  id INTEGER PRIMARY KEY,
  proposal_id INTEGER REFERENCES proposals(id),
  order_id TEXT, trade_id TEXT, qty INTEGER NOT NULL, price REAL NOT NULL,
  filled_at TEXT NOT NULL,
  source TEXT NOT NULL,                -- trades|holdings_diff|execute
  raw_json TEXT
);

CREATE TABLE kite_sessions (
  date TEXT PRIMARY KEY,               -- IST date of login
  user_id TEXT,
  access_token TEXT NOT NULL,          -- db file is chmod 600 and never committed
  logged_in_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE journal (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL, kind TEXT NOT NULL,   -- note|dissent|unplanned|refused|system
  symbol TEXT, ref_id TEXT, note TEXT NOT NULL
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  entry TEXT NOT NULL, step TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL,                -- running|ok|skipped|error
  detail TEXT
);

CREATE INDEX idx_prices_date ON prices(date);
CREATE INDEX idx_filings_symbol ON filings(symbol, filed_at);
CREATE INDEX idx_flags_status ON flags(status);
CREATE INDEX idx_proposals_status ON proposals(status);
CREATE INDEX idx_runs_entry ON runs(entry, step, started_at);
