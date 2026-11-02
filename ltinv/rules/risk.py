"""Portfolio risk limits (Architecture §7.2). Pure functions over positions.

Limits apply to individual stocks only (asset class `equity`), as a % of the whole portfolio
(all holdings + Kite cash). ETFs, SGBs and cash-like holdings count toward the total; cash =
cash-like holdings + Kite cash. A trade is blocked only for a breach it creates or worsens,
never for one that already existed and the trade leaves no worse (a sell always may proceed).
"""

from __future__ import annotations

from dataclasses import dataclass, field

LIMIT_KEYS = ("single_stock_max_pct", "sector_max_pct", "top5_max_pct", "min_cash_pct",
              "max_new_position_pct")


@dataclass
class Position:
    symbol: str
    asset_class: str | None
    sector: str | None
    value: float


@dataclass
class Breach:
    kind: str          # single_stock | sector | top5 | min_cash | new_position
    subject: str
    value_pct: float
    limit_pct: float

    def worse_than(self, other: "Breach | None") -> bool:
        if other is None:
            return True
        if self.kind == "min_cash":
            return self.value_pct < other.value_pct - 1e-9
        return self.value_pct > other.value_pct + 1e-9

    def text(self) -> str:
        op = "below" if self.kind == "min_cash" else "above"
        return f"{self.kind} {self.subject}: {self.value_pct:.1f}% is {op} the {self.limit_pct:g}% limit"


@dataclass
class Report:
    total: float
    cash: float
    cash_pct: float
    stock_weights: dict[str, float]
    sector_weights: dict[str, float]
    top5_pct: float
    breaches: list[Breach] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"total": round(self.total, 2), "cash": round(self.cash, 2), "cash_pct": round(self.cash_pct, 2),
                "stock_weights": {k: round(v, 2) for k, v in self.stock_weights.items()},
                "sector_weights": {k: round(v, 2) for k, v in self.sector_weights.items()},
                "top5_pct": round(self.top5_pct, 2),
                "breaches": [b.text() for b in self.breaches]}


def evaluate(positions: list[Position], kite_cash: float, limits: dict, new_symbol: str | None = None) -> Report:
    total = sum(p.value for p in positions) + kite_cash
    if total <= 0:
        return Report(0, kite_cash, 0, {}, {}, 0)
    pct = lambda v: v / total * 100  # noqa: E731
    cash = kite_cash + sum(p.value for p in positions if p.asset_class == "cash_like")
    stocks = {p.symbol: pct(p.value) for p in positions if p.asset_class == "equity" and p.value > 0}
    sectors: dict[str, float] = {}
    for p in positions:
        if p.asset_class == "equity" and p.value > 0:
            key = p.sector or "(no sector)"
            sectors[key] = sectors.get(key, 0) + pct(p.value)
    top5 = sum(sorted(stocks.values(), reverse=True)[:5])
    r = Report(total, cash, pct(cash), stocks, sectors, top5)
    for sym, w in stocks.items():
        if w > limits["single_stock_max_pct"]:
            r.breaches.append(Breach("single_stock", sym, w, limits["single_stock_max_pct"]))
    for sec, w in sectors.items():
        if w > limits["sector_max_pct"]:
            r.breaches.append(Breach("sector", sec, w, limits["sector_max_pct"]))
    if top5 > limits["top5_max_pct"]:
        r.breaches.append(Breach("top5", "top 5 stocks", top5, limits["top5_max_pct"]))
    if r.cash_pct < limits["min_cash_pct"]:
        r.breaches.append(Breach("min_cash", "cash", r.cash_pct, limits["min_cash_pct"]))
    if new_symbol and new_symbol in stocks and stocks[new_symbol] > limits["max_new_position_pct"]:
        r.breaches.append(Breach("new_position", new_symbol, stocks[new_symbol], limits["max_new_position_pct"]))
    return r


def simulate(positions: list[Position], kite_cash: float, limits: dict, symbol: str, side: str,
             value: float, asset_class: str = "equity", sector: str | None = None) -> tuple[Report, Report, list[Breach]]:
    """(before, after, blocking): blocking = breaches the trade creates or worsens."""
    before = evaluate(positions, kite_cash, limits)
    held = {p.symbol: p for p in positions}
    new = symbol not in held and side == "BUY"
    after_pos = [Position(p.symbol, p.asset_class, p.sector, p.value) for p in positions]
    if symbol in held:
        for p in after_pos:
            if p.symbol == symbol:
                p.value = max(0.0, p.value + (value if side == "BUY" else -value))
    elif side == "BUY":
        after_pos.append(Position(symbol, asset_class, sector, value))
    cash_after = kite_cash + (-value if side == "BUY" else value)
    after = evaluate(after_pos, cash_after, limits, new_symbol=symbol if new else None)
    prior = {(b.kind, b.subject): b for b in before.breaches}
    blocking = [b for b in after.breaches if b.worse_than(prior.get((b.kind, b.subject)))]
    return before, after, blocking
