"""Match what you actually traded in Kite back to approved proposals (Architecture §10).

v1 never places orders: you place them yourself. Each Kite sync (needs that day's login):
1. stores today's delivery (CNC) trades; intraday MIS trades (the day-trader fork shares the
   account) are ignored here and netted out of tax lots
2. links each trade to an approved proposal for the same symbol and side -> fills; when the
   fills reach the proposal's qty it becomes `reconciled` (partial fills keep it `approved`)
3. a delivery trade with no approved proposal is journaled as `unplanned`
4. fallback for a day you didn't log in (Kite's trades call only returns today): an approved
   proposal with no trade recorded since approval is filled from how far the holding has moved
   since approval (approvals.held_qty), priced at last close. Unplanned trades are detected
   from the trades list only, so they need the day's login.
"""

from __future__ import annotations

import json
import sqlite3

from ltinv.actions import proposal
from ltinv.db import repo
from ltinv.fetch import tradebook
from ltinv.timeutil import iso, now_ist


def _approved(conn, symbol: str, side: str):
    return conn.execute(
        """SELECT p.* FROM proposals p JOIN approvals a ON a.proposal_id=p.id
           WHERE p.status='approved' AND p.symbol=? AND p.side=? ORDER BY a.approved_at""",
        (symbol, side)).fetchall()


def _fill(conn, pid, trade_id, order_id, qty, price, at, source, raw) -> None:
    conn.execute(
        """INSERT INTO fills(proposal_id, order_id, trade_id, qty, price, filled_at, source, raw_json)
           VALUES (?,?,?,?,?,?,?,?)""", (pid, order_id, trade_id, qty, price, at, source, json.dumps(raw)))


def _apply(conn, symbol, side, qty, price, at, source, trade_id=None, order_id=None, raw=None) -> list[str]:
    """Allocate a trade to approved proposals; returns event lines."""
    events, left = [], qty
    for p in _approved(conn, symbol, side):
        if not left:
            break
        need = p["qty"] - proposal.filled_qty(conn, p["id"])
        if need <= 0:
            continue
        take = min(left, need)
        _fill(conn, p["id"], trade_id, order_id, take, price, at, source, raw)
        left -= take
        if proposal.filled_qty(conn, p["id"]) >= p["qty"]:
            conn.execute("UPDATE proposals SET status='reconciled' WHERE id=?", (p["id"],))
            repo.add_journal(conn, "system", f"proposal {p['id']} reconciled: {side} {p['qty']} {symbol}",
                             symbol=symbol, ref_id=str(p["id"]), commit=False)
            events.append(f"proposal #{p['id']} reconciled ({side} {p['qty']} {symbol})")
        else:
            events.append(f"proposal #{p['id']} partly filled: {take} of {p['qty']} {symbol}")
    if left:
        _fill(conn, None, trade_id, order_id, left, price, at, source, raw)
        repo.add_journal(conn, "unplanned", f"{side} {left} {symbol} @ {price} with no approved proposal ({source})",
                         symbol=symbol, ref_id=trade_id, commit=False)
        events.append(f"UNPLANNED {side} {left} {symbol} @ {price}")
    return events


def from_trades(conn: sqlite3.Connection, trades: list[dict]) -> list[str]:
    """Kite trades for today, as returned by KiteClient.trades()."""
    delivery = [t for t in trades if t["product"] in ("CNC", "")]
    seen = {r[0] for r in conn.execute("SELECT trade_id FROM fills WHERE trade_id IS NOT NULL")}
    tradebook.store(conn, [dict(t) for t in trades], source="kite")
    events = []
    with conn:
        for t in delivery:
            if t["trade_id"] in seen:
                continue
            events += _apply(conn, t["symbol"], t["side"], t["qty"], t["price"], t["timestamp"] or iso(now_ist()),
                             "trades", t["trade_id"], t["order_id"], t)
    return events


def from_holdings(conn: sqlite3.Connection) -> list[str]:
    """Fallback for approved proposals with no trade recorded since approval."""
    events = []
    rows = conn.execute(
        """SELECT p.*, a.held_qty, a.approved_at FROM proposals p JOIN approvals a ON a.proposal_id=p.id
           WHERE p.status='approved' AND a.held_qty IS NOT NULL""").fetchall()
    with conn:
        for p in rows:
            if conn.execute("SELECT 1 FROM fills WHERE proposal_id=? AND source='trades'", (p["id"],)).fetchone():
                continue  # trades are authoritative once seen
            h = conn.execute("SELECT qty FROM holdings WHERE symbol=?", (p["symbol"],)).fetchone()
            now_qty = h["qty"] if h else 0
            moved = (p["held_qty"] - now_qty) if p["side"] == "SELL" else (now_qty - p["held_qty"])
            new = min(moved, p["qty"]) - proposal.filled_qty(conn, p["id"])
            if new <= 0:
                continue
            px = proposal.last_close(conn, p["symbol"]) or 0.0
            events += _apply(conn, p["symbol"], p["side"], new, px, iso(now_ist()), "holdings_diff",
                             raw={"held_at_approval": p["held_qty"], "held_now": now_qty})
    return events


def sync(conn: sqlite3.Connection, s, client) -> dict:
    """One Kite pass: cash, today's trades, holdings; reconcile from trades, then the fallback."""
    cash, raw = client.cash()
    with conn:
        conn.execute("INSERT OR REPLACE INTO account_snapshots(as_of, cash, raw_json) VALUES (?,?,?)",
                     (iso(now_ist()), cash, json.dumps(raw, sort_keys=True)))
    trades = client.trades()
    events = from_trades(conn, trades)
    n, gone = repo.replace_holdings(conn, client.holdings(), s.load("sectors"), iso(now_ist()),
                                    cash_like=s.load("risk").get("cash_like_symbols") or [])
    events += from_holdings(conn)
    return {"holdings": n, "sold_out": gone, "cash": cash, "trades": len(trades), "events": events}
