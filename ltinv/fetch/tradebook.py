"""Import Zerodha Console tradebook CSVs (Reports > Tradebook, segment Equity, one FY per file).

Kite's API only returns today's trades, so buy dates for older lots come from these exports.
Columns used: symbol, isin, trade_date, exchange, segment, trade_type, quantity, price,
trade_id. Re-importing the same file is harmless (unique per symbol + trade_id + side).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime

from ltinv.timeutil import iso, now_ist

REQUIRED = {"symbol", "trade_date", "trade_type", "quantity", "price", "trade_id"}


class TradebookError(Exception):
    pass


def _date(text: str) -> str:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19] if "T" in text else text, fmt).date().isoformat()
        except ValueError:
            continue
    raise TradebookError(f"unrecognised trade_date {text!r}")


def parse(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise TradebookError("empty file")
    fields = {f.strip().lower() for f in reader.fieldnames}
    if not REQUIRED <= fields:
        raise TradebookError(f"not a Console tradebook (missing {', '.join(sorted(REQUIRED - fields))})")
    out = []
    for raw in reader:
        r = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if r.get("segment") and r["segment"].upper() not in ("EQ", "EQUITY"):
            continue  # F&O, currency, commodity: not this suite's business
        side = r["trade_type"].upper()
        if side not in ("BUY", "SELL"):
            raise TradebookError(f"trade {r['trade_id']}: trade_type {r['trade_type']!r}")
        out.append({"trade_id": r["trade_id"], "symbol": r["symbol"].upper(),
                    "isin": (r.get("isin") or "").upper() or None, "trade_date": _date(r["trade_date"]),
                    "side": side, "qty": int(float(r["quantity"])), "price": float(r["price"]),
                    "exchange": r.get("exchange") or None})
    return out


def store(conn: sqlite3.Connection, rows: list[dict], source: str, product: str | None = None) -> int:
    now, n = iso(now_ist()), 0
    with conn:
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO trades(trade_id, symbol, isin, trade_date, side, qty, price,
                                                exchange, product, source, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (r["trade_id"], r["symbol"], r.get("isin"), r["trade_date"], r["side"], r["qty"], r["price"],
                 r.get("exchange"), r.get("product", product), source, now))
            n += cur.rowcount
    return n
