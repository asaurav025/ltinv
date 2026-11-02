"""Overview: Kite session, weights vs limits, open flags, last runs."""

import streamlit as st

from ltinv import portfolio
from ltinv.config import ConfigError
from ltinv.dashboard.common import db, identity, money
from ltinv.fetch import kite as kite_mod

s, conn = db()
who = identity()

st.title("Overview")
status = kite_mod.session_status(conn)
c1, c2 = st.columns([3, 1])
c1.write(f"**Kite:** {status}")
if not status.startswith("active"):
    try:
        c2.link_button("Log in to Kite", kite_mod.KiteClient.from_settings(s).login_url())
    except ConfigError:
        c2.caption("KITE_API_KEY not set on the host")
try:
    r = portfolio.risk_check(conn, s, store=False)
except (ValueError, ConfigError) as exc:
    st.error(str(exc))
    st.stop()
m = st.columns(4)
m[0].metric("Portfolio", money(r["total"]))
m[1].metric("Cash", f"{r['cash_pct']}%", help=f"{money(r['cash'])} incl. liquid ETFs and Kite cash")
m[2].metric("Top-5 stocks", f"{r['top5_pct']}%")
m[3].metric("Breaches", len(r["breaches"]))
for b in r["breaches"]:
    st.warning(b)
lim = s.load("risk")
st.subheader("Stocks vs limits")
st.dataframe([{"stock": k, "weight %": v, "limit %": lim["single_stock_max_pct"]}
              for k, v in sorted(r["stock_weights"].items(), key=lambda kv: -kv[1])],
             hide_index=True, width="stretch")
if r["sector_weights"]:
    st.dataframe([{"sector": k, "weight %": v, "limit %": lim["sector_max_pct"]}
                  for k, v in sorted(r["sector_weights"].items(), key=lambda kv: -kv[1])],
                 hide_index=True, width="stretch")

st.subheader("Red flags")
flags = conn.execute(
    """SELECT f.id, f.symbol, f.severity, f.rule_id, f.llm_confirmed, f.kill_criterion, fl.subject
       FROM flags f LEFT JOIN filings fl ON fl.id=f.filing_id WHERE f.status='open'
       ORDER BY CASE f.severity WHEN 'fatal' THEN 0 WHEN 'serious' THEN 1 ELSE 2 END""").fetchall()
if not flags:
    st.caption("No open flags.")
for f in flags:
    state = "confirmed" if f["llm_confirmed"] == 1 else "not judged yet: run red-flag-scan in Claude Code"
    kill = f" · kill criterion `{f['kill_criterion']}`" if f["kill_criterion"] else ""
    st.write(f"**{f['severity'].upper()}** {f['symbol']} `{f['rule_id']}` #{f['id']}{kill} — {state}  \n"
             f"{(f['subject'] or '')[:180]}")

st.subheader("Last runs")
runs = conn.execute(
    """SELECT entry || '/' || step AS step, status, finished_at, detail FROM runs
       WHERE id IN (SELECT MAX(id) FROM runs GROUP BY entry, step) ORDER BY finished_at DESC""").fetchall()
st.dataframe([dict(r) for r in runs], hide_index=True, width="stretch")
