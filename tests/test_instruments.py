import gzip
import io
import zipfile
from datetime import date

import pytest

from ltinv import instruments
from ltinv.db import repo
from ltinv.db.repo import Holding
from ltinv.fetch import bhavcopy
from tests.conftest import FIXTURES

NSE_CSV = (FIXTURES / "bhav_20261030.csv").read_text()
HEADER = NSE_CSV.splitlines()[0]
# Real BSE row (public market data): NSE Ltd is listed on BSE only.
BSE_CSV = HEADER + "\n" + (
    "2026-10-30,2026-10-30,CM,BSE,STK,544937,INE721I01024,NSE,A,,,,,National Stock Exchange of Ind,"
    "1782.95,1795.70,1775.20,1782.10,1782.10,1788.95,,1782.10,,,1595793,2849713583.00,63391,F1,1,,,,,\n"
    # same company as an NSE fixture row, different BSE ticker: must not be picked by ticker
    "2026-10-30,2026-10-30,CM,BSE,STK,500325,INE002A01018,RELIANCE,A,,,,,RELIANCE INDUSTRIES LTD,"
    "1170.00,1175.00,1150.00,1165.00,1165.00,1167.00,,1165.00,,,100,116500.00,10,F1,1,,,,,\n"
)


@pytest.mark.parametrize("symbol,isin,cash,expected", [
    ("INFY", "INE009A01021", [], "equity"),
    ("NIFTYETF", "INF204KB14I2", [], "fund"),
    ("SGBX-GB", "IN0020200104", [], "govt"),
    ("LIQUIDETF", "INF732E01037", ["liquidetf"], "cash_like"),  # override wins, case-insensitive
    ("ODD", None, [], "other"),
])
def test_asset_class(symbol, isin, cash, expected):
    assert instruments.asset_class(symbol, isin, cash) == expected


def test_replace_holdings_sets_asset_class(conn):
    repo.replace_holdings(conn, [
        Holding("INFY", "NSE", "INE009A01021", 1, 1.0),
        Holding("SGBX-GB", "NSE", "IN0020200104", 1, 1.0),
        Holding("LIQUIDETF", "BSE", "INF732E01037", 1, 1.0),
    ], {}, "2026-11-02T18:30:00+05:30", cash_like=["LIQUIDETF"])
    got = {r["symbol"]: r["asset_class"] for r in repo.holdings(conn)}
    assert got == {"INFY": "equity", "SGBX-GB": "govt", "LIQUIDETF": "cash_like"}


class FakeHTTP:
    def __init__(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x.csv", NSE_CSV)
        self.nse_zip = buf.getvalue()
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)

        class R:
            pass

        r = R()
        r.status_code, r.content = 404, b""
        if "20261030" in url:
            if "nseindia" in url:
                r.status_code, r.content = 200, self.nse_zip
            elif "bseindia" in url:
                r.status_code, r.content = 200, ("﻿" + BSE_CSV).encode()
        return r


def test_bse_fallback_only_for_symbols_nse_lacks(conn, tmp_path):
    http = FakeHTTP()
    targets = {"RELIANCE": "INE002A01018", "NSE": "INE721I01024"}
    s = bhavcopy.fetch_prices(conn, tmp_path, targets, [date(2026, 10, 30)],
                              today=date(2026, 11, 9), http=http, pause_s=0)
    assert s.from_bse == {"NSE"} and s.unmatched == set()
    rows = {r["symbol"]: (r["c"], r["source"]) for r in conn.execute("SELECT * FROM prices")}
    assert rows == {"RELIANCE": (1164.9, "bhavcopy"), "NSE": (1782.1, "bhavcopy_bse")}
    bse_file = bhavcopy.cache_path(tmp_path, date(2026, 10, 30), bhavcopy.BSE)
    assert bse_file.name.endswith(".csv.gz")
    assert gzip.decompress(bse_file.read_bytes()).startswith(b"\xef\xbb\xbfTradDt")

    # second run: everything cached or already in the DB, no network
    before = len(http.calls)
    bhavcopy.fetch_prices(conn, tmp_path, targets, [date(2026, 10, 30)],
                          today=date(2026, 11, 9), http=http, pause_s=0)
    assert len(http.calls) == before


def test_bse_matches_isin_only():
    rows = bhavcopy.parse(BSE_CSV)
    picked = bhavcopy.match(rows, {"RELIANCE": None}, match_ticker=False)
    assert picked == []


def test_legacy_cache_layout_is_adopted_not_refetched(conn, tmp_path):
    day = date(2026, 10, 30)
    legacy = tmp_path / "bhavcopy" / "2026" / "BhavCopy_NSE_CM_20261030.csv.zip"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(FakeHTTP().nse_zip)
    (legacy.parent / "BhavCopy_NSE_CM_20261029.csv.missing").touch()
    http = FakeHTTP()
    s = bhavcopy.fetch_prices(conn, tmp_path, {"TCS": None}, [date(2026, 10, 29), day],
                              today=date(2026, 11, 9), http=http, pause_s=0)
    assert http.calls == [] and s.downloaded == 0 and s.rows_written == 1
    assert bhavcopy.cache_path(tmp_path, day).exists() and not legacy.exists()
    assert bhavcopy.missing_path(tmp_path, date(2026, 10, 29)).exists()
