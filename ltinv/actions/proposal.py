"""Proposals: a checked, expiring suggestion to trade, with its evidence (Architecture §10).

A proposal is never an order. In v1 you approve it (Phase 2) and place the trade yourself in
Kite. Created only from evidence: a confirmed fatal/serious flag, a hit on a thesis kill
criterion, or a manual `proposal create --evidence`.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta

from ltinv.db import repo
from ltinv.timeutil import iso, now_ist

SIDES = ("BUY", "SELL")
STUB = json.dumps({"status": "stub", "until": "phase 2"})


class ProposalError(Exception):
    pass


def last_close(conn: sqlite3.Connection, symbol: str) -> float | None:
    row = conn.execute("SELECT c FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
    return row["c"] if row else None


def create(conn: sqlite3.Connection, symbol: str, side: str, qty: int, reason: str,
           evidence: dict, band_pct: float = 3.0, ttl_hours: int = 24) -> tuple[int, bool]:
    """Returns (proposal id, created). An open proposal with the same symbol, side and reason
    is returned instead of a duplicate."""
    side = side.upper()
    if side not in SIDES:
        raise ProposalError(f"side must be BUY or SELL, not {side!r}")
    if qty <= 0:
        raise ProposalError("qty must be positive")
    if not evidence:
        raise ProposalError("a proposal needs evidence (a flag, review or file path)")
    dup = conn.execute("SELECT id FROM proposals WHERE symbol=? AND side=? AND reason=? AND status='open'",
                       (symbol, side, reason)).fetchone()
    if dup:
        return dup["id"], False
    close = last_close(conn, symbol)
    low = round(close * (1 - band_pct / 100), 2) if close else None
    high = round(close * (1 + band_pct / 100), 2) if close else None
    now = now_ist()
    with conn:
        cur = conn.execute(
            """INSERT INTO proposals(created_at, expires_at, symbol, side, qty, limit_low, limit_high,
                                     reason, evidence_json, tax_json, risk_json, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')""",
            (iso(now), iso(now + timedelta(hours=ttl_hours)), symbol, side, qty, low, high, reason,
             json.dumps(evidence, sort_keys=True), STUB, STUB),
        )
        pid = cur.lastrowid
        repo.add_journal(conn, "system", f"proposal {pid}: {side} {qty} {symbol} ({reason})",
                         symbol=symbol, ref_id=str(pid), commit=False)
    return pid, True


def from_flags(conn: sqlite3.Connection, trim_pct: float = 50) -> list[int]:
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
        pid, created = create(conn, f["symbol"], "SELL", qty, reason, evidence)
        if created:
            made.append(pid)
    return made


def expire(conn: sqlite3.Connection) -> list[int]:
    now = iso(now_ist())
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM proposals WHERE status='open' AND expires_at < ?", (now,))]
    with conn:
        for pid in ids:
            conn.execute("UPDATE proposals SET status='expired' WHERE id=?", (pid,))
            repo.add_journal(conn, "system", f"proposal {pid} expired unapproved", ref_id=str(pid), commit=False)
    return ids


def listing(conn: sqlite3.Connection, status: str | None = "open") -> list[sqlite3.Row]:
    if status:
        return conn.execute("SELECT * FROM proposals WHERE status=? ORDER BY id DESC", (status,)).fetchall()
    return conn.execute("SELECT * FROM proposals ORDER BY id DESC").fetchall()
