"""Reference data that maps feed items to your symbols.

- NSE's list of equities (EQUITY_L.csv): company name -> symbol, ISIN. Refreshed daily.
- BSE scrip codes: from the cached BSE bhavcopy (FinInstrmId is the scrip code), by ISIN.
"""

from __future__ import annotations

import csv
import glob
import io
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import requests

from ltinv.fetch import bhavcopy
from ltinv.instruments import needs_thesis

EQUITY_L_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
_SUFFIXES = re.compile(r"\b(limited|ltd|the|company|co|corporation|corp)\b")


class RefDataError(Exception):
    pass


def norm_name(name: str) -> str:
    """'Universal Cables Limited' == 'UNIVERSAL CABLES LTD.' == 'universal cables'."""
    n = name.lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = _SUFFIXES.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def equity_list(cache_dir: Path, today: date, http: requests.Session | None = None) -> list[dict]:
    """NSE equities [{symbol, name, isin}], cached under cache/ref/, refreshed once a day.
    A failed refresh falls back to the cached copy."""
    path = cache_dir / "ref" / "EQUITY_L.csv"
    fresh = path.exists() and datetime.fromtimestamp(path.stat().st_mtime).date() >= today
    if not fresh:
        try:
            resp = (http or requests.Session()).get(
                EQUITY_L_URL, headers={"User-Agent": bhavcopy.UA,
                                       "Referer": "https://www.nseindia.com/"}, timeout=30)
            if resp.status_code == 200 and resp.content.lstrip().startswith(b"SYMBOL"):
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".part")
                tmp.write_bytes(resp.content)
                tmp.replace(path)
        except requests.RequestException:
            pass
    if not path.exists():
        raise RefDataError("NSE equity list unavailable (no cached copy and download failed)")
    rows = []
    for r in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        rows.append({"symbol": r["SYMBOL"].upper(), "name": r["NAME OF COMPANY"],
                     "isin": r["ISIN NUMBER"].upper()})
    return rows


def bse_scripcodes(cache_dir: Path) -> dict[str, str]:
    """ISIN -> BSE scrip code, from the newest cached BSE bhavcopy ({} if none cached)."""
    files = sorted(glob.glob(str(cache_dir / "bhavcopy" / "BSE" / "*" / "*.csv.gz")))
    if not files:
        return {}
    text = bhavcopy.read_cached(Path(files[-1]))
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("ISIN") and r.get("FinInstrmId"):
            out[r["ISIN"].strip().upper()] = r["FinInstrmId"].strip()
    return out


def tracked_equities(conn: sqlite3.Connection, isin_by_symbol: dict[str, str] | None = None) -> dict[str, str | None]:
    """Symbols watched for filings and red flags: equity holdings plus anything with a thesis."""
    out: dict[str, str | None] = {}
    for r in conn.execute("SELECT symbol, isin, asset_class FROM holdings"):
        if needs_thesis(r["asset_class"]):
            out[r["symbol"]] = r["isin"]
    for r in conn.execute("SELECT symbol FROM theses"):
        out.setdefault(r["symbol"], (isin_by_symbol or {}).get(r["symbol"]))
    return out
