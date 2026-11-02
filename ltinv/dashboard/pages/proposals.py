"""Proposals: approve or reject; approved ones show the order to place in Kite."""

import json

import streamlit as st

from ltinv.actions import proposal
from ltinv.dashboard.common import db, identity, money, tax_summary

s, conn = db()
who = identity()

st.title("Proposals")
if not who:
    st.warning("No Cloudflare Access identity on this request: viewing only, approvals disabled.")
proposal.expire(conn)
rows = proposal.listing(conn, "open") + proposal.listing(conn, "approved")
if not rows:
    st.caption("Nothing waiting for you.")
for p in rows:
    ev = json.loads(p["evidence_json"] or "{}")
    tax = json.loads(p["tax_json"] or "{}")
    risk = json.loads(p["risk_json"] or "{}")
    band = f" · limit {p['limit_low']}–{p['limit_high']}" if p["limit_low"] else ""
    with st.container(border=True):
        st.subheader(f"#{p['id']} {p['side']} {p['qty']} {p['symbol']}{band}")
        st.caption(f"{p['reason']} · status {p['status']} · expires {p['expires_at']}")
        if ev.get("rationale"):
            st.write(ev["rationale"])
        if ev.get("filing_url"):
            st.link_button("Read the filing", ev["filing_url"])
        tax_summary(tax)
        for b in risk.get("blocking", []):
            st.error(f"Risk: {b}")
        if p["status"] == "approved":
            a = conn.execute("SELECT * FROM approvals WHERE proposal_id=?", (p["id"],)).fetchone()
            done = proposal.filled_qty(conn, p["id"])
            st.success(f"Approved until {a['valid_until']}. Place it in Kite yourself: {p['side']} {p['qty']} "
                       f"{p['symbol']}, LIMIT within {p['limit_low']}–{p['limit_high']}, CNC. "
                       f"Filled so far: {done}.")
            continue
        note = st.text_input("Note (why; required to override a risk breach or to reject)", key=f"note{p['id']}")
        c1, c2 = st.columns(2)
        if c1.button("Approve", key=f"ok{p['id']}", type="primary", disabled=not who):
            try:
                out = proposal.approve(conn, p["id"], s, channel="dashboard", by=who, note=note)
                st.success(f"Approved until {out['valid_until']}. Now place in Kite: {out['order']}")
            except proposal.ProposalError as exc:
                st.error(str(exc))
        if c2.button("Reject", key=f"no{p['id']}", disabled=not who):
            try:
                proposal.reject(conn, p["id"], note, by=who)
                st.info("Rejected and journaled.")
            except proposal.ProposalError as exc:
                st.error(str(exc))
