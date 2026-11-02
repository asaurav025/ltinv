# Deploying ltinv on the always-on host

The host runs deterministic code only (no LLM, no AI credentials). Paths assume user
`trader`, code in `~/ltinv`, personal state in `~/ltinv-home`; adjust if yours differ.

## 1. Code and cron

```sh
git clone https://github.com/asaurav025/ltinv ~/ltinv && cd ~/ltinv
python3 -m venv .venv && .venv/bin/pip install -e ".[dashboard]"
mkdir -p ~/ltinv-home && LTINV_HOME=~/ltinv-home .venv/bin/ltinv init
mkdir -p ~/ltinv-home/logs && (crontab -l 2>/dev/null; cat cron/ltinv.cron) | crontab -
```

## 2. Dashboard service

`ltinv dashboard` serves Streamlit on **127.0.0.1:8501 only**. Install the unit in
`deploy/ltinv-dashboard.service` (its header has the commands), then check it listens on
loopback and nothing else.

## 3. Cloudflare: hostname + Access (dashboard)

In Cloudflare Zero Trust, on the tunnel the host already runs:

1. **Access application first** (never publish the route without it): Access > Applications >
   Add > Self-hosted, domain `ltinv.<your domain>`, session 24 h, one **Allow** policy: your email
   (One-time PIN or Google). No service token, no Bypass rule.
2. **Published application route**: `ltinv.<your domain>` -> `http://127.0.0.1:8501`, with
   *Protect with Access* on.
3. Check from a browser with no Access cookie (private window): you must get the Access login,
   never the dashboard.

The dashboard reads the `Cf-Access-Authenticated-User-Email` header Access adds, records it as
who approved, and disables Approve/Reject on any request without it.

## 4. Kite login from the dashboard

In developers.kite.trade, set the **ltinv** app's redirect URL to `https://ltinv.<your domain>/`.
The Overview page then shows a "Log in to Kite" button when there is no session today; Kite
sends you back with a `request_token`, which the dashboard exchanges and stores. Pass the
Access login first (open the dashboard) so the redirect isn't diverted to the PIN page.

`ltinv kite login` (paste the URL) keeps working as a fallback.

## 5. Tax lots

Kite's API returns only today's trades, so buy dates for older lots come from Console:
console.zerodha.com > Reports > Tradebook > segment **Equity**, one file per financial year
since your first trade. Import each (from your machine, no copy needed on the host):

```sh
ssh <host> '~/ltinv/.venv/bin/ltinv --home ~/ltinv-home import tradebook -' < tradebook-EQ-2025_2026.csv
```

`ltinv tax show <SYMBOL>` then shows lots, STCG/LTCG, the FY exemption left and wait hints.
