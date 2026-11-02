"""Proposals: a checked, expiring suggestion to trade, with its evidence (Architecture §10).

A proposal is never an order. You approve it (dashboard or CLI), place the trade yourself in
Kite, and reconciliation matches the trade back. Created only from evidence: a confirmed
fatal/serious flag, a hit on a thesis kill criterion, or a manual `proposal create --evidence`.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta

from ltinv import calendar
from ltinv.db import repo
from ltinv.timeutil import iso, now_ist

SIDES = ("BUY", "SELL")


class ProposalError(Exception):
    pass


def last_close(conn: sqlite3.Connection, symbol: str) -> float | None:
    row = conn.execute("SELECT c FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
    return row["c"] if row else None


def checks(conn: sqlite3.Connection, s, symbol: str, side: str, qty: int) -> tuple[dict, dict]:
    """(tax, risk) views for a trade; {"status": "n/a"} when settings aren't available."""
    if s is None:
        na = {"status": "n/a", "why": "no settings"}
        return na, na
    from ltinv import portfolio
    try:
        risk = portfolio.risk_for_trade(conn, s, symbol, side, qty, None)
    except Exception as exc:  # a broken risk config must not hide the proposal
        risk = {"status": "error", "why": str(exc)}
    tax = portfolio.tax_for_sale(conn, s, symbol, qty, None) if side == "SELL" else {"status": "n/a", "why": "buy"}
    return tax, risk


def create(conn: sqlite3.Connection, symbol: str, side: str, qty: int, reason: str,
           evidence: dict, band_pct: float = 3.0, ttl_hours: int = 24, s=None) -> tuple[int, bool]:
    """Returns (proposal id, created). An open proposal with the same symbol, side and reason
    is returned instead of a duplicate."""
    side = side.upper()
    if side not in SIDES:
        raise ProposalError(f"side must be BUY or SELL, not {side!r}")
    if qty <= 0:
        raise ProposalError("qty must be positive")
    if not evidence:
        raise ProposalError("a proposal needs evidence (a flag, review or file path)")
    dup = conn.execute("SELECT id FROM proposals WHERE symbol=? AND side=? AND reason=? AND status IN ('open','approved')",
                       (symbol, side, reason)).fetchone()
    if dup:
        return dup["id"], False
    close = last_close(conn, symbol)
    low = round(close * (1 - band_pct / 100), 2) if close else None
    high = round(close * (1 + band_pct / 100), 2) if close else None
    tax, risk = checks(conn, s, symbol, side, qty)
    now = now_ist()
    with conn:
        cur = conn.execute(
            """INSERT INTO proposals(created_at, expires_at, symbol, side, qty, limit_low, limit_high,
                                     reason, evidence_json, tax_json, risk_json, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')""",
            (iso(now), iso(now + timedelta(hours=ttl_hours)), symbol, side, qty, low, high, reason,
             json.dumps(evidence, sort_keys=True), json.dumps(tax, sort_keys=True), json.dumps(risk, sort_keys=True)),
        )
        pid = cur.lastrowid
        repo.add_journal(conn, "system", f"proposal {pid}: {side} {qty} {symbol} ({reason})",
                         symbol=symbol, ref_id=str(pid), commit=False)
    return pid, True


def from_flags(conn: sqlite3.Connection, trim_pct: float = 50, s=None) -> list[int]:
    """Proposals for confirmed, open flags that are fatal, serious, or a thesis kill criterion.
    Fatal and kill criteria -> SELL full; serious -> SELL trim_pct of the holding."""
    made = []
    rows = conn.execute(
        """SELECT f.*, h.qty AS held, fl.url AS filing_url, fl.subject AS filing_subject
           FROM flags f JOIN holdings h ON h.symbol=f.symbol
           LEFT JOIN filings fl ON fl.id=f.filing_id
           WHERE f.status='open' AND f.llm_confirmed=1
             AND (f.severity IN ('fatal','serious') OR f.kill_criterion IS NOT NULL)""").fetchall()
    for f in rows:
        full = f["severity"] == "fatal" or f["kill_criterion"] is not None
        qty = f["held"] if full else max(1, math.floor(f["held"] * trim_pct / 100))
        reason = f"kill-criterion:{f['kill_criterion']}" if f["kill_criterion"] else f"red-flag:{f['rule_id']}"
        evidence = {"flag_id": f["id"], "rule": f["rule_id"], "severity": f["severity"],
                    "rationale": f["llm_rationale"], "filing_url": f["filing_url"],
                    "filing_subject": f["filing_subject"]}
        pid, created = create(conn, f["symbol"], "SELL", qty, reason, evidence, s=s)
        if created:
            made.append(pid)
    return made


def urgent(p: sqlite3.Row) -> bool:
    """Fatal flags and thesis kill criteria get a 1-trading-day approval window."""
    if p["reason"].startswith("kill-criterion:"):
        return True
    try:
        return json.loads(p["evidence_json"] or "{}").get("severity") == "fatal"
    except ValueError:
        return False


def approve(conn: sqlite3.Connection, pid: int, s, *, channel: str, by: str | None = None,
            note: str | None = None, at: datetime | None = None) -> dict:
    """Approve an open proposal. Re-runs the risk check; with block_on_breach, a breach the
    trade creates or worsens refuses approval unless `note` explains the override."""
    p = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if p is None:
        raise ProposalError(f"no proposal #{pid}")
    now = at or now_ist()
    if p["status"] != "open":
        raise ProposalError(f"proposal #{pid} is {p['status']}, not open")
    if datetime.fromisoformat(p["expires_at"]) <= now:
        expire(conn, at=now)
        raise ProposalError(f"proposal #{pid} expired at {p['expires_at']}; it needs fresh evidence")
    _, risk = checks(conn, s, p["symbol"], p["side"], p["qty"])
    note = (note or "").strip() or None
    if risk.get("blocking") and risk.get("block_on_breach", True) and not note:
        raise ProposalError("risk limits: " + "; ".join(risk["blocking"]) +
                            ". Approve with a note explaining the override, or reject.")
    valid_until = calendar.close_after_trading_days(now, 1 if urgent(p) else 5)
    held = conn.execute("SELECT qty FROM holdings WHERE symbol=?", (p["symbol"],)).fetchone()
    with conn:
        conn.execute(
            """INSERT INTO approvals(proposal_id, approved_at, valid_until, channel, risk_override_note,
                                     approved_by, note, held_qty) VALUES (?,?,?,?,?,?,?,?)""",
            (pid, iso(now), iso(valid_until), channel, note if risk.get("blocking") else None, by, note,
             held["qty"] if held else 0))
        conn.execute("UPDATE proposals SET status='approved', risk_json=? WHERE id=?",
                     (json.dumps(risk, sort_keys=True), pid))
        repo.add_journal(conn, "system", f"proposal {pid} approved via {channel}"
                         + (f" by {by}" if by else "") + (f"; override: {note}" if risk.get("blocking") else ""),
                         symbol=p["symbol"], ref_id=str(pid), commit=False)
    return {"proposal_id": pid, "valid_until": iso(valid_until), "risk": risk,
            "order": f"{p['side']} {p['qty']} {p['symbol']} LIMIT within {p['limit_low']}–{p['limit_high']}, CNC"}


def reject(conn: sqlite3.Connection, pid: int, note: str, by: str | None = None) -> None:
    p = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if p is None:
        raise ProposalError(f"no proposal #{pid}")
    if p["status"] not in ("open", "approved"):
        raise ProposalError(f"proposal #{pid} is {p['status']}")
    if not note.strip():
        raise ProposalError("say why you reject it (the journal scores decisions later)")
    with conn:
        conn.execute("UPDATE proposals SET status='rejected' WHERE id=?", (pid,))
        repo.add_journal(conn, "note", f"proposal {pid} rejected" + (f" by {by}" if by else "") + f": {note}",
                         symbol=p["symbol"], ref_id=str(pid), commit=False)


def expire(conn: sqlite3.Connection, at: datetime | None = None) -> list[int]:
    """Open proposals past expires_at, and approvals past valid_until that were never placed."""
    now = iso(at or now_ist())
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM proposals WHERE status='open' AND expires_at < ?", (now,))]
    stale = [r["id"] for r in conn.execute(
        """SELECT p.id FROM proposals p JOIN approvals a ON a.proposal_id=p.id
           WHERE p.status='approved' AND a.valid_until < ?""", (now,))]
    with conn:
        for pid in ids:
            conn.execute("UPDATE proposals SET status='expired' WHERE id=?", (pid,))
            repo.add_journal(conn, "system", f"proposal {pid} expired unapproved", ref_id=str(pid), commit=False)
        for pid in stale:
            conn.execute("UPDATE proposals SET status='expired' WHERE id=?", (pid,))
            repo.add_journal(conn, "system", f"proposal {pid} approved but not placed in time; expired",
                             ref_id=str(pid), commit=False)
    return ids + stale


def filled_qty(conn: sqlite3.Connection, pid: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(qty),0) FROM fills WHERE proposal_id=?", (pid,)).fetchone()[0]


def listing(conn: sqlite3.Connection, status: str | None = "open") -> list[sqlite3.Row]:
    if status:
        return conn.execute("SELECT * FROM proposals WHERE status=? ORDER BY id DESC", (status,)).fetchall()
    return conn.execute("SELECT * FROM proposals ORDER BY id DESC").fetchall()
