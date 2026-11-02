# ltinv — Long-Term Investor Suite

[![tests](https://github.com/asaurav025/ltinv/actions/workflows/tests.yml/badge.svg)](https://github.com/asaurav025/ltinv/actions/workflows/tests.yml)

A deterministic Python CLI for fundamentals-driven, long-term investing on NSE/BSE with
Zerodha. It keeps a written thesis per holding with pre-committed, machine-checkable sell
rules, watches exchange filings for governance red flags, and (in later phases) reviews
quarterly results against the thesis and turns conclusions into proposals you approve.

Rules, valuation, tax and risk checks are plain code with tests; an LLM is used at exactly
three call sites, with schema-validated output. In v1 the suite approves decisions and
**you place trades yourself in Kite**: it never places orders.

> **Not investment advice.** This is a personal project, shared as-is under the MIT
> license. It makes no recommendations to anyone, and nothing in it is a solicitation to
> buy or sell securities. Verify everything it tells you before acting on it.

## Design

- **Human decides and places, suite records.** Every trade needs an approval bound to one proposal.
- **Code first, LLM last.** Fetching, rules, valuation, tax and risk are pure Python.
- **Fundamentals trigger exits, price never does.** A price shock triggers a read, not a sell.
- **No unattended broker access.** Kite needs a manual login each trading day; pipelines
  that would need it skip cleanly instead of failing.

| Phase | Outcome | Status |
| --- | --- | --- |
| 0 | Repo, config, DB, Kite login + holdings, bhavcopy prices, first thesis | **done** |
| 1 | Filings feed, daily red-flag loop, proposals | next |
| 2 | Risk check, approvals, reconcile, Streamlit dashboard | |
| 3 | Monthly loop: shareholding, XBRL financials, metrics, valuation, health card | |
| 4 | Quarterly loop: concall, LLM review, journal scoring | |
| 5 | Dashboard completion, Claude Code skills, hardening | |

## Commands (Phase 0)

| Command | What it does |
| --- | --- |
| `ltinv init` | Creates `config/` (from examples) and `data/` (its own git repo), applies DB migrations. Safe to re-run; never overwrites your config. |
| `ltinv status` | DB, Kite session, holdings, last close, theses, open proposals, last runs. |
| `ltinv kite login` | Prints the Kite login link; paste the URL you land on to store today's session. |
| `ltinv fetch holdings` | Snapshots Kite holdings. Skipped (exit 0) without today's session. |
| `ltinv fetch prices [SYM...] --range 1y` | Daily closes from NSE bhavcopy, cached locally. |
| `ltinv thesis new\|show\|edit\|validate SYM` | Thesis in `data/thesis/SYM.md`; `validate` stores it only if every kill criterion is machine-checkable. |

## Setup

Code and personal state are kept apart: this repo is public, while your config, secrets
and data live in a separate folder (`LTINV_HOME`) that never enters it.

```sh
git clone https://github.com/asaurav025/ltinv && cd ltinv
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

mkdir -p ~/ltinv-home && export LTINV_HOME=~/ltinv-home
cp .env.example "$LTINV_HOME/.env"     # fill KITE_API_KEY / KITE_API_SECRET
.venv/bin/ltinv init                   # writes $LTINV_HOME/config and $LTINV_HOME/data
```

Back up `$LTINV_HOME/data` to a **private** remote. See [SECURITY.md](SECURITY.md) for
what lives where.

### Kite

Create a Kite Connect app on the free **Personal** plan (holdings, trades and orders; no
market data, which is why prices come from bhavcopy). Its redirect URL can be any URL you
control; the page doesn't have to load, since `ltinv kite login` only needs the
`request_token` from the address bar. Sessions expire at 06:00 IST.

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

## License

[MIT](LICENSE)
