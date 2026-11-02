"""Shared helpers for the dashboard pages."""

from __future__ import annotations

import os

import streamlit as st

from ltinv.config import get_settings
from ltinv.db import repo


def identity() -> str | None:
    """Cloudflare Access's verified email, or LTINV_DASHBOARD_LOCAL_USER for an SSH tunnel."""
    try:
        email = st.context.headers.get("Cf-Access-Authenticated-User-Email")
    except Exception:
        email = None
    return email or os.environ.get("LTINV_DASHBOARD_LOCAL_USER") or None


def db():
    s = get_settings()
    if not s.db_path.exists():
        st.error(f"No database at {s.db_path}. Run `ltinv init` on the host.")
        st.stop()
    return s, repo.connect(s.db_path)


def money(v: float) -> str:
    return f"\u20b9{v:,.0f}"


def tax_summary(tax: dict) -> None:
    if tax.get("status") != "ok":
        st.caption(f"Tax: {tax.get('why', 'n/a')}")
        return
    st.write(f"**Tax (approx.):** STCG {money(tax['stcg'])}, LTCG {money(tax['ltcg'])}, "
             f"estimated tax {money(tax['estimated_tax'])}; LTCG exemption left {money(tax['exemption_left_before'])}")
    for h in tax.get("wait_hints", []):
        st.info(f"Waiting {h['wait_days']} days turns {h['qty']} shares long-term and saves about {money(h['saves_inr'])}.")
    if tax.get("lots_mismatch"):
        st.caption(tax["lots_mismatch"])


