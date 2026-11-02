"""ltinv dashboard (Streamlit v1): Overview and Proposals (Architecture §8).

Run with `ltinv dashboard` on the host; it binds 127.0.0.1 only, so Cloudflare Tunnel + Access
is the way in. Writes go through ltinv.actions only, never raw SQL. Approving needs the
Cloudflare Access identity header (Cf-Access-Authenticated-User-Email), which is also recorded
as who approved; set LTINV_DASHBOARD_LOCAL_USER to approve over an SSH port-forward instead.
"""

from __future__ import annotations

import streamlit as st

from ltinv.config import ConfigError
from ltinv.dashboard.common import db
from ltinv.fetch import kite as kite_mod

st.set_page_config(page_title="ltinv", page_icon="📈", layout="wide")


s, conn = db()

# Kite login callback: point the Kite app's redirect URL at this dashboard.
qp = st.query_params
if qp.get("request_token"):
    try:
        out = kite_mod.login(s, conn, f"?status={qp.get('status', '')}&request_token={qp['request_token']}")
        st.success(f"Kite session stored for {out['user_id']}, valid until {out['expires_at']}.")
    except (kite_mod.KiteError, ConfigError) as exc:
        st.error(f"Kite login failed: {exc}")
    st.query_params.clear()

st.navigation([st.Page("pages/overview.py", title="Overview", default=True),
               st.Page("pages/proposals.py", title="Proposals")]).run()
