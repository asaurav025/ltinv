# ltinv — Long-Term Investor Suite

A deterministic Python CLI for fundamentals-driven, long-term investing on NSE/BSE with
Zerodha. Rules, valuation, tax and risk checks are plain code with tests; the LLM is used
at exactly three call sites (Phase 1+). In v1 the suite approves decisions and **you place
trades yourself in Kite**; it never places orders.

Design: the "ltinv — Long-Term Investor Suite" doc (Architecture, Plan, Skills, Feasibility tabs).

## Status: Phase 0 (foundation)

| Command | What it does |
| --- | --- |
| `ltinv init` | Checks `config/`, creates `data/` (its own git repo), applies DB migrations. Safe to re-run. |
| `ltinv status` | DB, Kite session, holdings, last close, theses, open proposals, last runs. |
| `ltinv kite login` | Prints the Kite login link; paste the URL you land on to store today's session. |
| `ltinv fetch holdings` | Snapshots Kite holdings. Skipped (exit 0) without today's session. |
| `ltinv fetch prices [SYM...] --range 1y` | Daily closes from NSE bhavcopy, cached under `data/cache/`. |
| `ltinv thesis new\|show\|edit\|validate SYM` | Written thesis in `data/thesis/SYM.md`; `validate` stores it in the DB only if every kill criterion is machine-checkable. |

## Setup

```sh
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env        # fill KITE_API_KEY / KITE_API_SECRET
.venv/bin/ltinv init
```

`config/` and `data/` are found under `LTINV_HOME` (or `--home`), defaulting to the
current directory. `data/` is gitignored here and is its own private repo; `ltinv.db`
(mode 600, holds the day's Kite token) and `data/cache/` are never committed.

### Kite

Create a Kite Connect app on the free **Personal** plan (holdings, trades and orders; no
market data, which is why prices come from bhavcopy). Set its redirect URL to any URL you
control; the page doesn't have to load, since `ltinv kite login` only needs the
`request_token` from the address bar. Sessions expire at 06:00 IST, so log in once each
trading day you want holdings synced. Automated TOTP login is deliberately not supported.

ltinv talks to Kite over plain HTTPS (`ltinv/fetch/kite.py`) rather than `pykiteconnect`,
which pulls in Twisted for a live ticker ltinv never uses.

### First stock

```sh
ltinv kite login
ltinv fetch holdings && ltinv fetch prices --range 1y
ltinv thesis new INFY       # fill it in
ltinv thesis validate INFY
ltinv status
```

## Tests

```sh
.venv/bin/pytest            # no network; CI runs with TZ=America/New_York
```
