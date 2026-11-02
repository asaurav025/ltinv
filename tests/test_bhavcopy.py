import io
import zipfile
from datetime import date

import pytest

from ltinv.db import repo
from ltinv.fetch import bhavcopy
from tests.conftest import FIXTURES

CSV = (FIXTURES / "bhav_20261030.csv").read_text()


def zip_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BhavCopy_NSE_CM_0_0_0_20261030_F_0000.csv", text)
    return buf.getvalue()


class FakeHTTP:
    """Serves the fixture for 2026-10-30, 404 for everything else; counts calls."""

    def __init__(self, files=None):
        self.files = files or {"20261030": zip_bytes(CSV)}
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        key = url.split("_0_0_0_")[1][:8]

        class R:
            pass

        r = R()
        if key in self.files:
            r.status_code, r.content = 200, self.files[key]
        else:
            r.status_code, r.content = 404, b"<html>not found</html>"
        return r


def test_parse_real_fixture():
    rows = bhavcopy.parse(CSV)
    rel = next(r for r in rows if r["symbol"] == "RELIANCE")
    assert rel == {"date": "2026-10-30", "symbol": "RELIANCE", "series": "EQ",
                   "isin": "INE002A01018", "o": 1170.4, "h": 1174.6, "l": 1152.5,
                   "c": 1164.9, "volume": 14753650}


def test_parse_rejects_unknown_format():
    with pytest.raises(bhavcopy.BhavcopyError):
        bhavcopy.parse("DATE,SYMBOL,CLOSE\n2026-10-30,RELIANCE,1\n")


def test_match_by_isin_then_ticker_and_ignores_non_equity():
    rows = bhavcopy.parse(CSV)
    # INFY tracked under an old name by ISIN; TCS by ticker; SGBJUN28 is a gold bond (GB)
    picked = bhavcopy.match(rows, {"INFOSYS": "INE009A01021", "TCS": None, "SGBJUN28": None})
    got = {r["symbol"]: r["c"] for r in picked}
    assert got == {"INFOSYS": 1185.0, "TCS": 2502.6}


def test_fetch_prices_caches_and_remembers_holidays(conn, tmp_path):
    http = FakeHTTP()
    days = [date(2026, 10, 29), date(2026, 10, 30)]
    s = bhavcopy.fetch_prices(conn, tmp_path, {"RELIANCE": None}, days,
                              today=date(2026, 11, 9), http=http, pause_s=0)
    assert (s.days_with_file, s.downloaded, s.rows_written, s.latest_date) == (1, 1, 1, "2026-10-30")
    assert s.unmatched == set()
    assert len(http.calls) == 2
    # 10-29 is older than 3 days with no file -> remembered as missing, never re-requested
    assert bhavcopy.missing_path(tmp_path, date(2026, 10, 29)).exists()

    s2 = bhavcopy.fetch_prices(conn, tmp_path, {"RELIANCE": None, "TCS": None}, days,
                               today=date(2026, 11, 9), http=http, pause_s=0)
    assert len(http.calls) == 2  # cache hit for 10-30, holiday marker for 10-29
    assert s2.rows_written == 2
    closes = dict(conn.execute("SELECT symbol, c FROM prices").fetchall())
    assert closes == {"RELIANCE": 1164.9, "TCS": 2502.6}


def test_todays_missing_file_is_not_remembered(conn, tmp_path):
    http = FakeHTTP(files={})
    day = date(2026, 11, 2)
    bhavcopy.fetch_prices(conn, tmp_path, {"RELIANCE": None}, [day], today=day, http=http, pause_s=0)
    bhavcopy.fetch_prices(conn, tmp_path, {"RELIANCE": None}, [day], today=day, http=http, pause_s=0)
    assert len(http.calls) == 2  # retried: NSE publishes today's file in the evening


def test_skips_days_already_in_db(conn, tmp_path):
    repo.upsert_prices(conn, [{"symbol": "RELIANCE", "date": "2026-10-30", "o": 1, "h": 1,
                               "l": 1, "c": 1, "volume": 1, "source": "kite"}])
    http = FakeHTTP()
    bhavcopy.fetch_prices(conn, tmp_path, {"RELIANCE": None}, [date(2026, 10, 30)],
                          today=date(2026, 11, 9), http=http, pause_s=0)
    assert http.calls == []


@pytest.mark.parametrize("text,days", [("1y", 365), ("6m", 183), ("30d", 30), ("2w", 14), ("90", 90)])
def test_parse_range(text, days):
    assert bhavcopy.parse_range(text) == days


def test_days_for_skips_weekends():
    days = bhavcopy.days_for(date(2026, 11, 2), 7)  # Tue 27 Oct .. Mon 2 Nov
    assert [d.isoformat() for d in days] == ["2026-10-27", "2026-10-28", "2026-10-29",
                                             "2026-10-30", "2026-11-02"]
