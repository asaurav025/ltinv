"""Daily closes from NSE's end-of-day CM bhavcopy (UDiFF format, July 2024 onwards).

One zip per trading day at
  https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
Files are cached under data/cache/bhavcopy/YYYY/ so a back-fill is downloaded once.
A weekday with no file is a holiday (or, for today, not yet published: NSE posts it in
the evening). Rows are matched to symbols by ISIN first, then by ticker for equity series.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable

import requests

from ltinv.db import repo

URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ltinv/0.1",
    "Referer": "https://www.nseindia.com/all-reports",
    "Accept": "*/*",
}
EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}
MISSING_AFTER_DAYS = 3  # a weekday older than this with no file is remembered as a holiday


class BhavcopyError(Exception):
    pass


def _num(text: str) -> float | None:
    text = (text or "").strip()
    return float(text) if text else None


def parse(csv_text: str) -> list[dict]:
    """All cash-market rows of one bhavcopy, normalised."""
    rows = []
    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"TradDt", "TckrSymb", "SctySrs", "ISIN", "ClsPric"}
    if not reader.fieldnames or not required <= set(reader.fieldnames):
        raise BhavcopyError(f"unexpected bhavcopy header: {reader.fieldnames}")
    for r in reader:
        close = _num(r["ClsPric"])
        if close is None:
            continue
        rows.append({
            "date": r["TradDt"].strip(),
            "symbol": r["TckrSymb"].strip().upper(),
            "series": r["SctySrs"].strip().upper(),
            "isin": r["ISIN"].strip().upper(),
            "o": _num(r.get("OpnPric", "")),
            "h": _num(r.get("HghPric", "")),
            "l": _num(r.get("LwPric", "")),
            "c": close,
            "volume": int(float(r["TtlTradgVol"])) if (r.get("TtlTradgVol") or "").strip() else None,
        })
    return rows


def match(rows: Iterable[dict], targets: dict[str, str | None]) -> list[dict]:
    """Pick rows for target symbols ({symbol: isin or None}); one row per symbol per day."""
    by_isin = {isin: sym for sym, isin in targets.items() if isin}
    picked: dict[tuple[str, str], dict] = {}
    for r in rows:
        sym = by_isin.get(r["isin"])
        if sym is None and r["symbol"] in targets and r["series"] in EQUITY_SERIES:
            sym = r["symbol"]
        if sym is None:
            continue
        key = (sym, r["date"])
        if key in picked and picked[key]["_series"] == "EQ":
            continue  # prefer the EQ row if a symbol shows up in two series
        picked[key] = {"symbol": sym, "date": r["date"], "o": r["o"], "h": r["h"], "l": r["l"],
                       "c": r["c"], "volume": r["volume"], "source": "bhavcopy", "_series": r["series"]}
    return [{k: v for k, v in row.items() if k != "_series"} for row in picked.values()]


def read_zip(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise BhavcopyError(f"{path}: no CSV inside")
        return zf.read(names[0]).decode("utf-8-sig")


def cache_path(cache_dir: Path, day: date) -> Path:
    return cache_dir / "bhavcopy" / f"{day:%Y}" / f"BhavCopy_NSE_CM_{day:%Y%m%d}.csv.zip"


def download(day: date, cache_dir: Path, today: date, http: requests.Session) -> Path | None:
    """Cached zip for `day`, downloading it if needed. None = no file (holiday / not yet out)."""
    path = cache_path(cache_dir, day)
    missing = path.with_suffix(".missing")
    if path.exists():
        return path
    if missing.exists():
        return None
    try:
        resp = http.get(URL.format(d=day), headers=HEADERS, timeout=30)
    except requests.RequestException as exc:
        raise BhavcopyError(f"{day}: {exc}") from exc
    if resp.status_code == 404 or (resp.status_code == 200 and not resp.content.startswith(b"PK")):
        if (today - day).days > MISSING_AFTER_DAYS:
            missing.parent.mkdir(parents=True, exist_ok=True)
            missing.touch()
        return None
    if resp.status_code != 200:
        raise BhavcopyError(f"{day}: HTTP {resp.status_code} from NSE archives")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(path)
    return path


@dataclass
class FetchSummary:
    days_checked: int = 0
    days_with_file: int = 0
    downloaded: int = 0
    rows_written: int = 0
    latest_date: str | None = None
    unmatched: set[str] = field(default_factory=set)


def fetch_prices(
    conn: sqlite3.Connection,
    cache_dir: Path,
    targets: dict[str, str | None],
    days: Iterable[date],
    today: date,
    http: requests.Session | None = None,
    pause_s: float = 0.4,
    progress: Callable[[str], None] | None = None,
) -> FetchSummary:
    """Back-fill closes for `targets` over `days`. Skips days every target already has."""
    http = http or requests.Session()
    have = repo.price_dates(conn, targets)
    seen: set[str] = set()
    s = FetchSummary()
    for day in days:
        s.days_checked += 1
        key = day.isoformat()
        if all(key in have[sym] for sym in targets):
            s.days_with_file += 1
            seen.update(targets)
            s.latest_date = max(s.latest_date or key, key)
            continue
        was_cached = cache_path(cache_dir, day).exists()
        path = download(day, cache_dir, today, http)
        if path is None:
            continue
        if not was_cached:
            s.downloaded += 1
            if pause_s:
                time.sleep(pause_s)  # be gentle with nsearchives
        s.days_with_file += 1
        rows = match(parse(read_zip(path)), targets)
        s.rows_written += repo.upsert_prices(conn, rows)
        seen.update(r["symbol"] for r in rows)
        s.latest_date = max(s.latest_date or key, key)
        if progress:
            progress(f"{key}: {len(rows)} rows")
    s.unmatched = set(targets) - seen
    return s


def parse_range(text: str) -> int:
    """'1y' -> 365, '6m' -> 182, '30d' -> 30, '90' -> 90 (calendar days)."""
    text = text.strip().lower()
    units = {"d": 1, "w": 7, "m": 30.5, "y": 365}
    try:
        if text[-1] in units:
            return max(1, round(float(text[:-1]) * units[text[-1]]))
        return max(1, int(text))
    except (ValueError, IndexError):
        raise BhavcopyError(f"bad range {text!r}; use e.g. 30d, 6m, 1y") from None


def days_for(end: date, span_days: int) -> list[date]:
    start = end - timedelta(days=span_days - 1)
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out
