"""Thin query layer over sqlite3: connection, migrations, and the writes Phase 0 needs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterable, Iterator

from ltinv.timeutil import iso, now_ist

_MIGRATION_RE = re.compile(r"^(\d{4})_[\w-]+\.sql$")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    new = not db_path.exists()
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if new:
        os.chmod(db_path, 0o600)  # holds the day's Kite access token
    return conn


def migrations() -> list[tuple[int, str, str]]:
    folder = resources.files("ltinv.db") / "migrations"
    found = []
    for entry in folder.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.name, entry.read_text()))
    return sorted(found)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns the names applied."""
    current = schema_version(conn)
    applied = []
    for number, name, sql in migrations():
        if number <= current:
            continue
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        applied.append(name)
    return applied


# --- runs -----------------------------------------------------------------------------


@contextmanager
def run_step(conn: sqlite3.Connection, entry: str, step: str) -> Iterator[dict]:
    """Records a pipeline step in `runs`. The body may set info['status'] = 'skipped'
    and info['detail']; an exception marks the run 'error' and re-raises."""
    cur = conn.execute(
        "INSERT INTO runs(entry, step, started_at, status) VALUES (?,?,?, 'running')",
        (entry, step, iso(now_ist())),
    )
    run_id = cur.lastrowid
    conn.commit()
    info: dict = {"status": "ok", "detail": None}
    try:
        yield info
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE runs SET finished_at=?, status='error', detail=? WHERE id=?",
            (iso(now_ist()), f"{type(exc).__name__}: {exc}", run_id),
        )
        conn.commit()
        raise
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, detail=? WHERE id=?",
        (iso(now_ist()), info["status"], info["detail"], run_id),
    )
    conn.commit()


# --- holdings -------------------------------------------------------------------------


@dataclass(frozen=True)
class Holding:
    symbol: str
    exchange: str | None
    isin: str | None
    qty: int
    avg_price: float | None


def replace_holdings(
    conn: sqlite3.Connection, holdings: Iterable[Holding], sectors: dict[str, str], as_of: str,
    cash_like: Iterable[str] = (),
) -> tuple[int, list[str]]:
    """Make `holdings` match the broker snapshot. Keeps first_buy_date; sector comes from
    config/sectors.yaml, asset class from the ISIN (see ltinv.instruments).
    Returns (rows written, symbols no longer held)."""
    from ltinv.instruments import asset_class

    cash_like = list(cash_like)
    rows = [h for h in holdings if h.qty > 0]
    symbols = {h.symbol for h in rows}
    before = {r["symbol"] for r in conn.execute("SELECT symbol FROM holdings")}
    with conn:
        for h in rows:
            conn.execute(
                """INSERT INTO holdings(symbol, exchange, isin, qty, avg_price, sector, as_of,
                                        asset_class)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET exchange=excluded.exchange,
                     isin=excluded.isin, qty=excluded.qty, avg_price=excluded.avg_price,
                     sector=excluded.sector, as_of=excluded.as_of,
                     asset_class=excluded.asset_class""",
                (h.symbol, h.exchange, h.isin, h.qty, h.avg_price, sectors.get(h.symbol), as_of,
                 asset_class(h.symbol, h.isin, cash_like)),
            )
        gone = sorted(before - symbols)
        for sym in gone:
            conn.execute("DELETE FROM holdings WHERE symbol=?", (sym,))
            add_journal(conn, "system", f"{sym} no longer in Kite holdings", symbol=sym, commit=False)
    return len(rows), gone


def holdings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM holdings ORDER BY symbol").fetchall()


# --- prices ---------------------------------------------------------------------------


def upsert_prices(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    n = 0
    with conn:
        for r in rows:
            conn.execute(
                """INSERT INTO prices(symbol, date, o, h, l, c, volume, source)
                   VALUES (:symbol, :date, :o, :h, :l, :c, :volume, :source)
                   ON CONFLICT(symbol, date) DO UPDATE SET o=excluded.o, h=excluded.h,
                     l=excluded.l, c=excluded.c, volume=excluded.volume, source=excluded.source""",
                r,
            )
            n += 1
    return n


def price_dates(conn: sqlite3.Connection, symbols: Iterable[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for sym in symbols:
        out[sym] = {r[0] for r in conn.execute("SELECT date FROM prices WHERE symbol=?", (sym,))}
    return out


# --- kite sessions --------------------------------------------------------------------


def save_kite_session(
    conn: sqlite3.Connection, day: str, user_id: str | None, access_token: str,
    logged_in_at: str, expires_at: str,
) -> None:
    with conn:
        conn.execute(
            """INSERT INTO kite_sessions(date, user_id, access_token, logged_in_at, expires_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET user_id=excluded.user_id,
                 access_token=excluded.access_token, logged_in_at=excluded.logged_in_at,
                 expires_at=excluded.expires_at""",
            (day, user_id, access_token, logged_in_at, expires_at),
        )


def latest_kite_session(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM kite_sessions ORDER BY logged_in_at DESC LIMIT 1").fetchone()


# --- theses ---------------------------------------------------------------------------


def upsert_thesis(
    conn: sqlite3.Connection, symbol: str, path: str, target_weight_pct: float | None,
    kill_criteria: list[dict], metrics: list[dict], now: str,
) -> None:
    with conn:
        conn.execute(
            """INSERT INTO theses(symbol, path, created_at, updated_at, target_weight_pct,
                                  kill_criteria_json, metrics_json)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET path=excluded.path,
                 updated_at=excluded.updated_at, target_weight_pct=excluded.target_weight_pct,
                 kill_criteria_json=excluded.kill_criteria_json, metrics_json=excluded.metrics_json""",
            (symbol, path, now, now, target_weight_pct,
             json.dumps(kill_criteria, sort_keys=True), json.dumps(metrics, sort_keys=True)),
        )


# --- journal --------------------------------------------------------------------------


def add_journal(
    conn: sqlite3.Connection, kind: str, note: str, symbol: str | None = None,
    ref_id: str | None = None, commit: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO journal(ts, kind, symbol, ref_id, note) VALUES (?,?,?,?,?)",
        (iso(now_ist()), kind, symbol, ref_id, note),
    )
    if commit:
        conn.commit()
