"""Portfolio views assembled from the DB: positions at last close, Kite cash, tax books, and
the risk/tax checks a proposal carries."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date

from ltinv.config import Settings
from ltinv.rules import risk as risk_mod
from ltinv.rules import tax as tax_mod
from ltinv.timeutil import iso, now_ist, today_ist


def last_close(conn: sqlite3.Connection, symbol: str) -> float | None:
    row = conn.execute("SELECT c FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
    return row["c"] if row else None


def positions(conn: sqlite3.Connection) -> tuple[list[risk_mod.Position], list[str]]:
    """(positions valued at last close, symbols with no price, valued at average cost)."""
    out, unpriced = [], []
    for h in conn.execute("SELECT * FROM holdings"):
        px = last_close(conn, h["symbol"])
        if px is None:
            unpriced.append(h["symbol"])
            px = h["avg_price"] or 0
        out.append(risk_mod.Position(h["symbol"], h["asset_class"], h["sector"], h["qty"] * px))
    return out, unpriced


def kite_cash(conn: sqlite3.Connection) -> tuple[float, str | None]:
    row = conn.execute("SELECT cash, as_of FROM account_snapshots ORDER BY as_of DESC LIMIT 1").fetchone()
    return (row["cash"], row["as_of"]) if row else (0.0, None)


def limits(s: Settings) -> dict:
    cfg = s.load("risk")
    missing = [k for k in risk_mod.LIMIT_KEYS if k not in cfg]
    if missing:
        raise ValueError(f"config/risk.yaml is missing {', '.join(missing)}")
    return cfg


def risk_check(conn: sqlite3.Connection, s: Settings, store: bool = True) -> dict:
    pos, unpriced = positions(conn)
    cash, cash_as_of = kite_cash(conn)
    report = risk_mod.evaluate(pos, cash, limits(s)).as_dict()
    report.update(unpriced=unpriced, kite_cash_as_of=cash_as_of)
    if store:
        with conn:
            conn.execute("INSERT OR REPLACE INTO risk_snapshots(as_of, json) VALUES (?,?)",
                         (iso(now_ist()), json.dumps(report, sort_keys=True)))
    return report


def risk_for_trade(conn: sqlite3.Connection, s: Settings, symbol: str, side: str, qty: int,
                   price: float | None) -> dict:
    pos, _ = positions(conn)
    cash, _ = kite_cash(conn)
    px = price or last_close(conn, symbol) or 0
    h = conn.execute("SELECT asset_class, sector FROM holdings WHERE symbol=?", (symbol,)).fetchone()
    sector = h["sector"] if h else s.load("sectors").get(symbol)
    before, after, blocking = risk_mod.simulate(pos, cash, limits(s), symbol, side, qty * px,
                                                h["asset_class"] if h else "equity", sector)
    return {"trade_value": round(qty * px, 2), "blocking": [b.text() for b in blocking],
            "breaches_after": [b.text() for b in after.breaches],
            "cash_pct_after": round(after.cash_pct, 2),
            "weight_after": round(after.stock_weights.get(symbol, 0.0), 2),
            "block_on_breach": bool(s.load("risk").get("block_on_breach", True))}


def books(conn: sqlite3.Connection) -> dict[str, tax_mod.Book]:
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for t in conn.execute("SELECT * FROM trades WHERE COALESCE(product,'') != 'MIS' ORDER BY trade_date, id"):
        by_sym[t["symbol"]].append({"trade_date": date.fromisoformat(t["trade_date"]), "side": t["side"],
                                    "qty": t["qty"], "price": t["price"]})
    return {sym: tax_mod.build(ts) for sym, ts in by_sym.items()}


def tax_for_sale(conn: sqlite3.Connection, s: Settings, symbol: str, qty: int, price: float | None,
                 on: date | None = None) -> dict:
    h = conn.execute("SELECT asset_class, qty FROM holdings WHERE symbol=?", (symbol,)).fetchone()
    if h is None or h["asset_class"] != "equity":
        return {"status": "n/a", "why": "tax view covers listed equity shares only"}
    on = on or today_ist()
    px = price or last_close(conn, symbol)
    if px is None:
        return {"status": "n/a", "why": f"no price for {symbol}"}
    all_books = books(conn)
    book = all_books.get(symbol, tax_mod.Book())
    cfg = s.load("tax")
    out = tax_mod.sale(book, qty, px, on, cfg, cfg["ltcg_exemption_per_fy_inr"] - tax_mod.exemption_used(all_books, on))
    lot_qty = sum(l.qty for l in book.lots)
    if lot_qty != h["qty"]:
        out["lots_mismatch"] = (f"recorded lots cover {lot_qty} of {h['qty']} shares held: import the Console "
                                "tradebook (ltinv import tradebook) for older buys, bonuses or splits")
    out["status"] = "ok"
    return out
