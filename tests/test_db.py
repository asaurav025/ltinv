import os
import stat

import pytest

from ltinv.db import repo
from ltinv.db.repo import Holding


def test_migrate_is_idempotent(settings):
    c = repo.connect(settings.db_path)
    assert repo.migrate(c) == ["0001_init.sql"]
    assert repo.migrate(c) == []
    assert repo.schema_version(c) == 1
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"holdings", "prices", "theses", "proposals", "approvals", "fills",
            "kite_sessions", "journal", "runs"} <= tables


def test_db_file_is_private(settings):
    repo.connect(settings.db_path)
    mode = stat.S_IMODE(os.stat(settings.db_path).st_mode)
    assert mode == 0o600


def test_replace_holdings_upserts_and_drops(conn):
    sectors = {"INFY": "IT Services"}
    n, gone = repo.replace_holdings(conn, [
        Holding("INFY", "NSE", "INE009A01021", 10, 1500.0),
        Holding("TCS", "NSE", "INE467B01029", 5, 3000.0),
        Holding("ZERO", "NSE", None, 0, None),  # fully sold today: not a holding
    ], sectors, "2026-11-02T18:30:00+05:30")
    assert (n, gone) == (2, [])
    conn.execute("UPDATE holdings SET first_buy_date='2024-01-05' WHERE symbol='INFY'")
    conn.commit()

    n, gone = repo.replace_holdings(conn, [Holding("INFY", "NSE", "INE009A01021", 12, 1490.0)],
                                    sectors, "2026-11-03T18:30:00+05:30")
    assert (n, gone) == (1, ["TCS"])
    row = repo.holdings(conn)[0]
    assert (row["symbol"], row["qty"], row["sector"], row["first_buy_date"]) == \
        ("INFY", 12, "IT Services", "2024-01-05")
    notes = [r["note"] for r in conn.execute("SELECT note FROM journal")]
    assert notes == ["TCS no longer in Kite holdings"]


def test_run_step_records_ok_skipped_and_error(conn):
    with repo.run_step(conn, "fetch", "a"):
        pass
    with repo.run_step(conn, "fetch", "b") as info:
        info.update(status="skipped", detail="no session")
    with pytest.raises(RuntimeError):
        with repo.run_step(conn, "fetch", "c"):
            raise RuntimeError("boom")
    rows = {r["step"]: (r["status"], r["detail"]) for r in conn.execute("SELECT * FROM runs")}
    assert rows == {"a": ("ok", None), "b": ("skipped", "no session"),
                    "c": ("error", "RuntimeError: boom")}
