"""Asset classes, decided from the ISIN. Only equities carry a thesis, a sector and red flags;
funds (ETFs), government securities (incl. Sovereign Gold Bonds) and cash-like holdings count
toward portfolio value and the cash limit only."""

from __future__ import annotations

from typing import Iterable

EQUITY, FUND, GOVT, CASH_LIKE, OTHER = "equity", "fund", "govt", "cash_like", "other"
ORDER = (EQUITY, FUND, GOVT, CASH_LIKE, OTHER)


def asset_class(symbol: str, isin: str | None, cash_like: Iterable[str] = ()) -> str:
    """INE = company shares, INF = mutual-fund/ETF units, IN00 = government securities.
    Symbols listed as cash-like (e.g. liquid ETFs) win over the ISIN rule."""
    if symbol.upper() in {s.upper() for s in cash_like}:
        return CASH_LIKE
    isin = (isin or "").upper()
    if isin.startswith("INE"):
        return EQUITY
    if isin.startswith("INF"):
        return FUND
    if isin.startswith("IN00"):
        return GOVT
    return OTHER


def needs_thesis(cls: str | None) -> bool:
    return cls == EQUITY
