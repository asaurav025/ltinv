"""Capital-gains view of a sale, from your own trades (Architecture §7.3). Pure functions.

Listed equity shares, STT paid (sections 111A/112A), rates from config/tax.yaml:
- long-term when held for MORE than 12 months (sold after the anniversary of the buy date)
- STCG 20%; LTCG 12.5% above a Rs 1.25 lakh exemption per financial year (Apr-Mar),
  shared by all equity sales in that year.
Lots come from FIFO over your delivery trades. Same-day buy+sell quantity in one stock is
intraday (speculative), netted out first and never matched against older lots: the
day-trader-skill fork trades MIS on the same account.

Approximate by design: no grandfathering (pre-2018 lots), no set-off of losses against other
gains, no surcharge/cess, and ETF/SGB sales are not counted toward the exemption.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Lot:
    buy_date: date
    qty: int
    price: float


@dataclass
class Realized:
    sell_date: date
    qty: int
    gain: float
    long_term: bool


@dataclass
class Book:
    """Open lots and realized gains for one symbol."""
    lots: list[Lot] = field(default_factory=list)
    realized: list[Realized] = field(default_factory=list)
    unmatched_sell_qty: int = 0   # sells with no recorded buy (pre-import history, corporate action)


def is_long_term(buy: date, sell: date) -> bool:
    try:
        anniversary = buy.replace(year=buy.year + 1)
    except ValueError:  # 29 Feb
        anniversary = date(buy.year + 1, 3, 1)
    return sell > anniversary


def days_to_long_term(buy: date, on: date) -> int:
    d = 0
    probe = on
    while not is_long_term(buy, probe):
        probe = date.fromordinal(probe.toordinal() + 1)
        d += 1
    return d


def fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def build(trades: list[dict]) -> Book:
    """trades: [{trade_date: date, side, qty, price}] for ONE symbol, any order."""
    by_day: dict[date, list[dict]] = defaultdict(list)
    for t in trades:
        by_day[t["trade_date"]].append(t)
    book = Book()
    for day in sorted(by_day):
        buys = [t for t in by_day[day] if t["side"] == "BUY"]
        sells = [t for t in by_day[day] if t["side"] == "SELL"]
        bq, sq = sum(t["qty"] for t in buys), sum(t["qty"] for t in sells)
        intraday = min(bq, sq)
        if bq > intraday:  # delivery buy: the part not squared off today, at the avg buy price
            avg = sum(t["qty"] * t["price"] for t in buys) / bq
            book.lots.append(Lot(day, bq - intraday, avg))
        if sq > intraday:  # delivery sell: FIFO against older lots
            avg = sum(t["qty"] * t["price"] for t in sells) / sq
            left = sq - intraday
            while left and book.lots:
                lot = book.lots[0]
                take = min(left, lot.qty)
                book.realized.append(Realized(day, take, take * (avg - lot.price), is_long_term(lot.buy_date, day)))
                lot.qty -= take
                left -= take
                if lot.qty == 0:
                    book.lots.pop(0)
            book.unmatched_sell_qty += left
    return book


def exemption_used(books: dict[str, Book], on: date) -> float:
    """Net long-term gains already realized this financial year (floored at 0)."""
    start = fy_start(on)
    net = sum(r.gain for b in books.values() for r in b.realized if r.long_term and r.sell_date >= start)
    return max(0.0, net)


def sale(book: Book, qty: int, price: float, on: date, cfg: dict, exemption_left: float) -> dict:
    """Tax view of selling `qty` at `price` on `on`, FIFO from the book's open lots."""
    st_rate, lt_rate = cfg["stcg_rate_pct"] / 100, cfg["ltcg_rate_pct"] / 100
    hint_days = cfg.get("wait_hint_max_days", 45)
    left, stcg, ltcg, rows, hints = qty, 0.0, 0.0, [], []
    for lot in book.lots:
        if not left:
            break
        take = min(left, lot.qty)
        gain = take * (price - lot.price)
        lt = is_long_term(lot.buy_date, on)
        rows.append({"buy_date": lot.buy_date.isoformat(), "qty": take, "buy_price": round(lot.price, 2),
                     "gain": round(gain, 2), "term": "long" if lt else "short",
                     "days_held": (on - lot.buy_date).days})
        if lt:
            ltcg += gain
        else:
            stcg += gain
            wait = days_to_long_term(lot.buy_date, on)
            if gain > 0 and wait <= hint_days:
                hints.append({"qty": take, "wait_days": wait,
                              "saves_inr": round(gain * (st_rate - lt_rate), 0)})
        left -= take
    unknown = left  # shares sold beyond the recorded lots
    taxable_lt = max(0.0, ltcg - max(0.0, exemption_left)) if ltcg > 0 else 0.0
    tax = max(0.0, stcg) * st_rate + taxable_lt * lt_rate
    return {
        "qty": qty, "price": price, "lots": rows, "stcg": round(stcg, 2), "ltcg": round(ltcg, 2),
        "exemption_left_before": round(max(0.0, exemption_left), 2),
        "estimated_tax": round(tax, 2), "wait_hints": hints, "shares_without_lots": unknown,
        "note": "approximate: listed equity rates, no loss set-off, no surcharge/cess",
    }
