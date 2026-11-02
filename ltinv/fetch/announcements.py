"""Corporate announcements from the exchanges' public RSS feeds.

NSE  https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
     ~1,700 items/day; title = company name; description ends "|SUBJECT: <NSE category>".
BSE  https://www.bseindia.com/data/xml/announcements.xml
     ~11,000 items/day; title "<Name> (<scrip code>)", <scripcode>, free-text description.

Both hold only the current day, so `fetch filings` polls through the day; items are stored
once (unique per exchange + uid). NSE's website and BSE's JSON API block server IPs; these
feeds and the archives do not (checked from the VPS on 2026-11-02).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

import requests

from ltinv.timeutil import IST, iso

NSE_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
BSE_URL = "https://www.bseindia.com/data/xml/announcements.xml"
UA = "Mozilla/5.0 (X11; Linux x86_64) ltinv/0.1"
_NSE_HOST = "https://nsearchives.nseindia.com"


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class Announcement:
    exchange: str
    company: str
    scripcode: str | None
    category: str | None       # NSE's subject category; None for BSE
    subject: str               # what was filed, in the exchange's or company's words
    url: str | None
    filed_at: str              # ISO, IST
    uid: str


def _uid(*parts: str | None) -> str:
    return hashlib.sha1("\x1f".join(p or "" for p in parts).encode()).hexdigest()[:20]


def _when(text: str) -> str:
    try:
        return iso(datetime.strptime(text.strip(), "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST))
    except ValueError as exc:
        raise FeedError(f"bad pubDate {text!r}") from exc


def fix_nse_link(url: str | None) -> str | None:
    """Some NSE items drop '/corporate/' (https://nsearchives.nseindia.comFOO_...pdf)."""
    if url and url.startswith(_NSE_HOST) and not url[len(_NSE_HOST):].startswith("/"):
        return f"{_NSE_HOST}/corporate/{url[len(_NSE_HOST):]}"
    if url and "corporate-filings-announcements" in url:
        return None  # generic listing page, not a document
    return url


def _items(data: bytes) -> list[ET.Element]:
    try:
        root = ET.fromstring(data.decode("utf-8-sig", errors="replace"))
    except ET.ParseError as exc:
        raise FeedError(f"feed is not valid XML: {exc}") from exc
    return root.findall("./channel/item")


def parse_nse(data: bytes) -> list[Announcement]:
    out = []
    for it in _items(data):
        company = (it.findtext("title") or "").strip()
        desc = (it.findtext("description") or "").strip()
        subject, _, category = desc.partition("|SUBJECT:")
        link, when = it.findtext("link"), it.findtext("pubDate") or ""
        if not company or not when:
            continue
        out.append(Announcement(
            exchange="NSE", company=company, scripcode=None,
            category=category.strip() or None, subject=subject.strip(),
            url=fix_nse_link((link or "").strip() or None), filed_at=_when(when),
            uid=_uid(company, desc, when, link),
        ))
    return out


_BSE_TITLE = re.compile(r"^(.*?)\s*\((\d{5,7})\)\s*$")


def parse_bse(data: bytes) -> list[Announcement]:
    out = []
    for it in _items(data):
        title = (it.findtext("title") or "").strip()
        m = _BSE_TITLE.match(title)
        code = (it.findtext("scripcode") or (m.group(2) if m else "")).strip()
        desc = re.sub(r"\s+", " ", it.findtext("description") or "").strip()
        link, when = it.findtext("link"), it.findtext("pubDate") or ""
        if not code or not when:
            continue
        out.append(Announcement(
            exchange="BSE", company=m.group(1) if m else title, scripcode=code, category=None,
            subject=desc[:500], url=(link or "").strip() or None, filed_at=_when(when),
            uid=_uid(code, desc, when, link),
        ))
    return out


def download_feed(url: str, http: requests.Session) -> bytes:
    referer = "https://www.bseindia.com/" if "bseindia" in url else "https://www.nseindia.com/"
    try:
        resp = http.get(url, headers={"User-Agent": UA, "Referer": referer}, timeout=60)
    except requests.RequestException as exc:
        raise FeedError(f"{url}: {exc}") from exc
    if resp.status_code != 200:
        raise FeedError(f"{url}: HTTP {resp.status_code}")
    return resp.content


@dataclass
class CollectSummary:
    nse_items: int = 0
    bse_items: int = 0
    new: dict[str, int] | None = None
    not_covered: list[str] | None = None   # tracked symbols neither feed can map
    errors: list[str] | None = None


def collect(conn: sqlite3.Connection, cache_dir, today, http: requests.Session | None = None,
            nse_url: str = NSE_URL, bse_url: str = BSE_URL) -> CollectSummary:
    """Store today's filings for tracked equities: NSE feed for NSE-listed ones (matched by
    company name via EQUITY_L), BSE feed for BSE-only ones (matched by scrip code)."""
    from ltinv import refdata

    http = http or requests.Session()
    s = CollectSummary(new={}, not_covered=[], errors=[])
    eq = refdata.equity_list(cache_dir, today, http)
    targets = refdata.tracked_equities(conn, {r["symbol"]: r["isin"] for r in eq})
    if not targets:
        return s
    by_isin = {isin: sym for sym, isin in targets.items() if isin}
    nse_isins = {r["isin"] for r in eq}
    name_to_sym = {refdata.norm_name(r["name"]): by_isin[r["isin"]] for r in eq if r["isin"] in by_isin}
    for sym, isin in targets.items():  # tracked by symbol only (no ISIN yet): match NSE ticker
        if not isin:
            name_to_sym.update({refdata.norm_name(r["name"]): sym for r in eq if r["symbol"] == sym})
    bse_only = {isin: sym for isin, sym in by_isin.items() if isin not in nse_isins}
    code_to_sym: dict[str, str] = {}
    if bse_only:
        codes = refdata.bse_scripcodes(cache_dir)
        code_to_sym = {codes[i]: sym for i, sym in bse_only.items() if i in codes}
    covered = set(name_to_sym.values()) | set(code_to_sym.values())
    s.not_covered = sorted(set(targets) - covered)

    def take(items: list[Announcement], key) -> None:
        with conn:
            for a in items:
                sym = key(a)
                if sym and store(conn, sym, a):
                    s.new[sym] = s.new.get(sym, 0) + 1

    try:
        nse = parse_nse(download_feed(nse_url, http))
        s.nse_items = len(nse)
        take(nse, lambda a: name_to_sym.get(refdata.norm_name(a.company)))
    except FeedError as exc:
        s.errors.append(f"NSE feed: {exc}")
    if code_to_sym:
        try:
            bse = parse_bse(download_feed(bse_url, http))
            s.bse_items = len(bse)
            take(bse, lambda a: code_to_sym.get(a.scripcode or ""))
        except FeedError as exc:
            s.errors.append(f"BSE feed: {exc}")
    return s


def store(conn: sqlite3.Connection, symbol: str, a: Announcement) -> bool:
    """Insert one filing; False if already stored."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO filings(symbol, exchange, filed_at, category, subject, company,
                                         url, uid, raw_json, seen_by_rules)
           VALUES (?,?,?,?,?,?,?,?,NULL,0)""",
        (symbol, a.exchange, a.filed_at, a.category, a.subject, a.company, a.url, a.uid),
    )
    return cur.rowcount == 1
