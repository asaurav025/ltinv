"""Daily closes from the exchanges' end-of-day cash-market bhavcopy (UDiFF format, mid-2024 on).

NSE first, for every target symbol:
  https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
BSE second, only for targets NSE never had (BSE-only listings, e.g. NSE Ltd itself), by ISIN:
  https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV
Files are cached under data/cache/bhavcopy/<EXCH>/YYYY/ (BSE's plain CSV is stored gzipped),
so a back-fill is downloaded once. A weekday with no file is a holiday (or, for today, not yet
published: both exchanges post it in the evening).
"""

from __future__ import annotations

import csv
import gzip
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

UA = "Mozilla/5.0 (X11; Linux x86_64) ltinv/0.1"
NSE_EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}
MISSING_AFTER_DAYS = 3  # a weekday older than this with no file is remembered as a holiday


@dataclass(frozen=True)
class Exchange:
    name: str
    url: str
    referer: str
    zipped: bool          # NSE serves a zip, BSE a plain CSV
    match_ticker: bool    # NSE tickers equal Kite's; BSE's need not, so BSE matches ISIN only
    source: str           # prices.source value


NSE = Exchange("NSE", "https://nsearchives.nseindia.com/content/cm/"
               "BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
               "https://www.nseindia.com/all-reports", True, True, "bhavcopy")
BSE = Exchange("BSE", "https://www.bseindia.com/download/BhavCopy/Equity/"
               "BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV",
               "https://www.bseindia.com/", False, False, "bhavcopy_bse")


class BhavcopyError(Exception):
    pass


def _num(text: str) -> float | None:
    text = (text or "").strip()
    return float(text) if text else None


def parse(csv_text: str) -> list[dict]:
    """All cash-market rows of one bhavcopy (either exchange), normalised."""
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


def match(rows: Iterable[dict], targets: dict[str, str | None], match_ticker: bool = True,
          source: str = "bhavcopy") -> list[dict]:
    """Pick rows for target symbols ({symbol: isin or None}); one row per symbol per day.
    ISIN first; ticker (NSE equity series only) when `match_ticker`."""
    by_isin = {isin: sym for sym, isin in targets.items() if isin}
    picked: dict[tuple[str, str], dict] = {}
    for r in rows:
        sym = by_isin.get(r["isin"])
        if sym is None and match_ticker and r["symbol"] in targets and r["series"] in NSE_EQUITY_SERIES:
            sym = r["symbol"]
        if sym is None:
            continue
        key = (sym, r["date"])
        if key in picked and picked[key]["_series"] == "EQ":
            continue  # prefer the EQ row if a symbol shows up in two series
        picked[key] = {"symbol": sym, "date": r["date"], "o": r["o"], "h": r["h"], "l": r["l"],
                       "c": r["c"], "volume": r["volume"], "source": source, "_series": r["series"]}
    return [{k: v for k, v in row.items() if k != "_series"} for row in picked.values()]


def cache_path(cache_dir: Path, day: date, exch: Exchange = NSE) -> Path:
    ext = "csv.zip" if exch.zipped else "csv.gz"
    return cache_dir / "bhavcopy" / exch.name / f"{day:%Y}" / f"BhavCopy_{exch.name}_CM_{day:%Y%m%d}.{ext}"


def missing_path(cache_dir: Path, day: date, exch: Exchange = NSE) -> Path:
    return cache_dir / "bhavcopy" / exch.name / f"{day:%Y}" / f"BhavCopy_{exch.name}_CM_{day:%Y%m%d}.missing"


def _adopt_legacy(cache_dir: Path, day: date, path: Path, missing: Path) -> None:
    """v0.1 cached NSE files at bhavcopy/YYYY/ (no exchange folder); move them, don't refetch."""
    old_dir = cache_dir / "bhavcopy" / f"{day:%Y}"
    old_file = old_dir / f"BhavCopy_NSE_CM_{day:%Y%m%d}.csv.zip"
    old_missing = old_dir / f"BhavCopy_NSE_CM_{day:%Y%m%d}.csv.missing"
    for old, new in ((old_file, path), (old_missing, missing)):
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)


def read_cached(path: Path) -> str:
    if path.name.endswith(".gz"):
        return gzip.decompress(path.read_bytes()).decode("utf-8-sig")
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise BhavcopyError(f"{path}: no CSV inside")
        return zf.read(names[0]).decode("utf-8-sig")


def _looks_valid(exch: Exchange, content: bytes) -> bool:
    return content.startswith(b"PK") if exch.zipped else content.lstrip(b"\xef\xbb\xbf").startswith(b"TradDt")


def download(day: date, cache_dir: Path, today: date, http: requests.Session,
             exch: Exchange = NSE) -> Path | None:
    """Cached file for `day`, downloading it if needed. None = no file (holiday / not yet out)."""
    path = cache_path(cache_dir, day, exch)
    missing = missing_path(cache_dir, day, exch)
    if exch is NSE:
        _adopt_legacy(cache_dir, day, path, missing)
    if path.exists():
        return path
    if missing.exists():
        return None
    try:
        resp = http.get(exch.url.format(d=day), headers={"User-Agent": UA, "Referer": exch.referer,
                                                          "Accept": "*/*"}, timeout=30)
    except requests.RequestException as exc:
        raise BhavcopyError(f"{exch.name} {day}: {exc}") from exc
    if resp.status_code == 404 or (resp.status_code == 200 and not _looks_valid(exch, resp.content)):
        if (today - day).days > MISSING_AFTER_DAYS:
            missing.parent.mkdir(parents=True, exist_ok=True)
            missing.touch()
        return None
    if resp.status_code != 200:
        raise BhavcopyError(f"{exch.name} {day}: HTTP {resp.status_code}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(resp.content if exch.zipped else gzip.compress(resp.content))
    tmp.replace(path)
    return path


@dataclass
class FetchSummary:
    days_checked: int = 0
    days_with_file: int = 0
    downloaded: int = 0
    rows_written: int = 0
    latest_date: str | None = None
    from_bse: set[str] = field(default_factory=set)
    unmatched: set[str] = field(default_factory=set)


def _pass(conn, cache_dir, exch, targets, days, today, http, pause_s, progress,
          s: FetchSummary) -> tuple[set[str], int]:
    """One exchange over `days`. Returns (symbols found, trading days seen)."""
    have = repo.price_dates(conn, targets)
    seen: set[str] = set()
    trading_days = 0
    for day in days:
        key = day.isoformat()
        if all(key in have[sym] for sym in targets):
            seen.update(targets)
            trading_days += 1
            s.latest_date = max(s.latest_date or key, key)
            continue
        was_cached = cache_path(cache_dir, day, exch).exists() or (
            exch is NSE and (cache_dir / "bhavcopy" / f"{day:%Y}" /
                             f"BhavCopy_NSE_CM_{day:%Y%m%d}.csv.zip").exists())
        path = download(day, cache_dir, today, http, exch)
        if path is None:
            continue
        trading_days += 1
        if not was_cached:
            s.downloaded += 1
            if pause_s:
                time.sleep(pause_s)  # be gentle with the exchange archives
        rows = match(parse(read_cached(path)), targets, exch.match_ticker, exch.source)
        s.rows_written += repo.upsert_prices(conn, rows)
        seen.update(r["symbol"] for r in rows)
        if rows:
            s.latest_date = max(s.latest_date or key, key)
        if progress:
            progress(f"{exch.name} {key}: {len(rows)} rows")
    return seen, trading_days


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
    """Back-fill closes for `targets` over `days`: NSE for all, then BSE (by ISIN) for any
    symbol NSE never had in the range. Skips days a symbol already has."""
    http = http or requests.Session()
    days = list(days)
    s = FetchSummary(days_checked=len(days))
    seen, s.days_with_file = _pass(conn, cache_dir, NSE, targets, days, today, http, pause_s,
                                   progress, s)
    bse_targets = {sym: isin for sym, isin in targets.items() if sym not in seen and isin}
    if bse_targets:
        found, _ = _pass(conn, cache_dir, BSE, bse_targets, days, today, http, pause_s, progress, s)
        s.from_bse = found
        seen |= found
    s.unmatched = set(targets) - seen
    return s


def parse_range(text: str) -> int:
    """'1y' -> 365, '6m' -> 183, '30d' -> 30, '90' -> 90 (calendar days)."""
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
