"""Phase 2: calendar, tax lots, risk limits, tradebook import, approvals, reconciliation."""

import json
from datetime import date, datetime

import pytest

from ltinv import calendar, portfolio
from ltinv.actions import proposal, reconcile
from ltinv.db import repo
from ltinv.db.repo import Holding
from ltinv.fetch import kite, tradebook
from ltinv.rules import risk, tax
from ltinv.timeutil import IST

TAX = {"stcg_rate_pct": 20, "ltcg_rate_pct": 12.5, "ltcg_exemption_per_fy_inr": 125000, "wait_hint_max_days": 45}
LIMITS = {"single_stock_max_pct": 12, "sector_max_pct": 30, "top5_max_pct": 55, "min_cash_pct": 5,
          "max_new_position_pct": 5}


def ist(*a):
    return datetime(*a, tzinfo=IST)


# --- calendar ---------------------------------------------------------------------------


@pytest.mark.parametrize("start,n,expected", [
    (ist(2026, 11, 2, 10, 0), 1, ist(2026, 11, 2, 15, 30)),    # Mon morning -> today's close
    (ist(2026, 11, 2, 20, 0), 1, ist(2026, 11, 3, 15, 30)),    # Mon evening -> Tue close
    (ist(2026, 11, 6, 20, 0), 1, ist(2026, 11, 9, 15, 30)),    # Fri evening -> Mon (8th is Sun anyway)
    (ist(2026, 11, 9, 20, 0), 1, ist(2026, 11, 11, 15, 30)),   # skips Diwali-Balipratipada on the 10th
    (ist(2026, 11, 2, 20, 0), 5, ist(2026, 11, 9, 15, 30)),    # Tue..Fri + Mon
])
def test_close_after_trading_days(start, n, expected):
    assert calendar.close_after_trading_days(start, n) == expected


# --- tax --------------------------------------------------------------------------------


def T(d, side, qty, price):
    return {"trade_date": date.fromisoformat(d), "side": side, "qty": qty, "price": price}


def test_long_term_needs_more_than_12_months():
    assert not tax.is_long_term(date(2025, 11, 3), date(2026, 11, 3))
    assert tax.is_long_term(date(2025, 11, 3), date(2026, 11, 4))
    assert tax.is_long_term(date(2024, 2, 29), date(2025, 3, 2))
    assert tax.days_to_long_term(date(2025, 11, 3), date(2026, 11, 2)) == 2


def test_fifo_and_intraday_netting():
    book = tax.build([
        T("2025-06-02", "BUY", 10, 100),
        T("2026-01-05", "BUY", 10, 150),
        T("2026-03-10", "BUY", 5, 200), T("2026-03-10", "SELL", 5, 210),   # intraday, nets out
        T("2026-04-15", "SELL", 12, 180),                                  # FIFO: 10 old + 2 new
    ])
    assert [(l.buy_date.isoformat(), l.qty, l.price) for l in book.lots] == [("2026-01-05", 8, 150)]
    assert [(r.qty, r.gain, r.long_term) for r in book.realized] == [(10, 800.0, False), (2, 60.0, False)]
    assert book.unmatched_sell_qty == 0


def test_sale_view_exemption_and_wait_hint():
    book = tax.build([T("2025-06-02", "BUY", 10, 100), T("2025-11-20", "BUY", 10, 100)])
    out = tax.sale(book, 20, 1100.0, date(2026, 11, 2), TAX, exemption_left=5000.0)
    assert [r["term"] for r in out["lots"]] == ["long", "short"]
    assert (out["ltcg"], out["stcg"]) == (10000.0, 10000.0)
    assert out["estimated_tax"] == pytest.approx(10000 * 0.20 + (10000 - 5000) * 0.125)
    assert out["wait_hints"] == [{"qty": 10, "wait_days": 19, "saves_inr": 750.0}]


def test_exemption_used_counts_this_fy_only():
    books = {"A": tax.build([T("2024-01-01", "BUY", 10, 100), T("2025-03-10", "SELL", 5, 200),
                             T("2025-05-10", "SELL", 5, 300)])}
    assert tax.exemption_used(books, date(2025, 11, 1)) == 1000.0   # FY25-26: only the May sale


# --- risk -------------------------------------------------------------------------------


POS = [risk.Position("BIG", "equity", "Pharma", 15000), risk.Position("SMALL", "equity", "IT", 5000),
       risk.Position("ETF", "fund", None, 70000), risk.Position("LIQ", "cash_like", None, 5000)]


def test_evaluate_stocks_only():
    r = risk.evaluate(POS, kite_cash=5000, limits=LIMITS)
    assert r.total == 100000 and r.cash_pct == 10.0
    assert r.stock_weights == {"BIG": 15.0, "SMALL": 5.0}           # the 70% ETF is not a breach
    assert [b.kind for b in r.breaches] == ["single_stock"]


def test_sell_never_blocked_buy_can_be():
    _, after, blocking = risk.simulate(POS, 5000, LIMITS, "BIG", "SELL", 2000)
    assert blocking == [] and after.stock_weights["BIG"] == pytest.approx(13.0)
    _, _, blocking = risk.simulate(POS, 5000, LIMITS, "BIG", "BUY", 1000)    # worsens a breach
    assert [b.kind for b in blocking] == ["single_stock"]
    _, _, blocking = risk.simulate(POS, 5000, LIMITS, "NEW", "BUY", 6000, sector="Auto")
    assert {b.kind for b in blocking} == {"new_position", "min_cash"}   # cash 10% -> 4%


# --- tradebook --------------------------------------------------------------------------

CSV = """symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time
ACME,INE000A01011,2026-09-08,NSE,EQ,EQ,buy,false,6.000000,480.50,T1,O1,2026-09-08T10:01:02
ACME,INE000A01011,2026-10-13,NSE,EQ,EQ,buy,false,4.000000,520.40,T2,O2,2026-10-13T11:00:00
SGBDE31III-GB,IN0020200104,2026-01-05,NSE,EQ,GB,buy,false,1.000000,6100.00,T3,O3,2026-01-05T10:00:00
NIFTY26NOVFUT,,2026-10-01,NFO,FO,,buy,false,75,24000,T4,O4,2026-10-01T10:00:00
"""


def test_tradebook_parse_and_store_idempotent(conn):
    rows = tradebook.parse(CSV)
    assert [(r["symbol"], r["side"], r["qty"]) for r in rows] == [
        ("ACME", "BUY", 6), ("ACME", "BUY", 4), ("SGBDE31III-GB", "BUY", 1)]   # F&O skipped
    assert tradebook.store(conn, rows, "tradebook") == 3
    assert tradebook.store(conn, rows, "tradebook") == 0


def test_tradebook_rejects_other_files():
    with pytest.raises(tradebook.TradebookError, match="not a Console tradebook"):
        tradebook.parse("date,amount\n2026-01-01,5\n")


# --- portfolio + approvals ---------------------------------------------------------------


@pytest.fixture
def book(conn, settings):
    repo.replace_holdings(conn, [Holding("ACME", "NSE", "INE000A01011", 10, 500.0),
                                 Holding("NIFTYETF", "NSE", "INF000X01011", 100, 280.0),
                                 Holding("LIQ", "NSE", "INF000L01011", 10, 1000.0)],
                          {"ACME": "Industrials"}, "2026-11-02T18:00:00+05:30", cash_like=["LIQ"])
    repo.upsert_prices(conn, [{"symbol": s, "date": "2026-11-02", "o": 1, "h": 1, "l": 1, "c": c,
                               "volume": 1, "source": "bhavcopy"} for s, c in
                              (("ACME", 450.6), ("NIFTYETF", 280.0), ("LIQ", 1000.0))])
    tradebook.store(conn, tradebook.parse(CSV), "tradebook")
    with conn:
        conn.execute("INSERT INTO account_snapshots(as_of, cash) VALUES ('2026-11-02T18:00:00+05:30', 2000)")
    return conn


def test_tax_for_sale_from_db(book, settings):
    out = portfolio.tax_for_sale(book, settings, "ACME", 10, None, on=date(2026, 11, 2))
    assert out["status"] == "ok" and out["stcg"] == pytest.approx(10 * 450.6 - (6 * 480.5 + 4 * 520.4))
    assert "lots_mismatch" not in out and out["estimated_tax"] == 0.0     # a loss
    assert portfolio.tax_for_sale(book, settings, "NIFTYETF", 1, None)["status"] == "n/a"


def test_risk_check_stores_snapshot(book, settings):
    r = portfolio.risk_check(book, settings)
    assert r["total"] == pytest.approx(4506 + 28000 + 10000 + 2000)
    assert r["kite_cash_as_of"] and book.execute("SELECT COUNT(*) FROM risk_snapshots").fetchone()[0] == 1


def _proposal(conn, settings, reason="red-flag:kmp_exit_no_successor", severity="serious", qty=5):
    pid, _ = proposal.create(conn, "ACME", "SELL", qty, reason, {"flag_id": 1, "severity": severity}, s=settings)
    return pid


def test_create_carries_tax_and_risk(book, settings):
    pid = _proposal(book, settings)
    p = book.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    assert json.loads(p["tax_json"])["status"] == "ok"
    assert json.loads(p["risk_json"])["blocking"] == []


@pytest.mark.parametrize("reason,severity,days", [
    ("red-flag:kmp_exit_no_successor", "serious", ist(2026, 11, 9, 15, 30)),
    ("kill-criterion:profit_falls", "watch", ist(2026, 11, 3, 15, 30)),
    ("red-flag:auditor_resignation", "fatal", ist(2026, 11, 3, 15, 30)),
])
def test_approve_ttl(book, settings, reason, severity, days):
    pid = _proposal(book, settings, reason, severity)
    out = proposal.approve(book, pid, settings, channel="cli", at=ist(2026, 11, 2, 20, 0))
    assert out["valid_until"] == days.isoformat(timespec="seconds")
    assert "SELL 5 ACME LIMIT" in out["order"]
    a = book.execute("SELECT held_qty FROM approvals WHERE proposal_id=?", (pid,)).fetchone()
    assert a["held_qty"] == 10


def test_approve_refuses_expired_and_double(book, settings):
    pid, _ = proposal.create(book, "ACME", "SELL", 1, "x", {"a": 1}, ttl_hours=-1, s=settings)
    with pytest.raises(proposal.ProposalError, match="expired"):
        proposal.approve(book, pid, settings, channel="cli")
    assert book.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()[0] == "expired"
    pid = _proposal(book, settings)
    proposal.approve(book, pid, settings, channel="cli")
    with pytest.raises(proposal.ProposalError, match="approved, not open"):
        proposal.approve(book, pid, settings, channel="cli")


def test_risk_breach_needs_override_note(book, settings):
    pid, _ = proposal.create(book, "ACME", "BUY", 100, "valuation-add", {"path": "x"}, s=settings)
    with pytest.raises(proposal.ProposalError, match="risk limits: single_stock ACME"):
        proposal.approve(book, pid, settings, channel="dashboard")
    out = proposal.approve(book, pid, settings, channel="dashboard", note="Deliberate: averaging down after Q2")
    a = book.execute("SELECT * FROM approvals WHERE proposal_id=?", (pid,)).fetchone()
    assert out["risk"]["blocking"] and a["risk_override_note"].startswith("Deliberate")


def test_reject_needs_reason(book, settings):
    pid = _proposal(book, settings)
    with pytest.raises(proposal.ProposalError, match="say why"):
        proposal.reject(book, pid, "  ")
    proposal.reject(book, pid, "thesis still intact; CFO successor expected")
    assert book.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()[0] == "rejected"


def test_approved_but_unplaced_expires(book, settings):
    pid = _proposal(book, settings)
    proposal.approve(book, pid, settings, channel="cli", at=ist(2026, 11, 2, 20, 0))
    assert proposal.expire(book, at=ist(2026, 11, 9, 15, 29)) == []
    assert proposal.expire(book, at=ist(2026, 11, 9, 15, 31)) == [pid]


# --- reconcile --------------------------------------------------------------------------


def kt(tid, side, qty, product="CNC", symbol="ACME", price=1231.0):
    return {"trade_id": tid, "order_id": "o" + tid, "symbol": symbol, "exchange": "NSE", "side": side,
            "qty": qty, "price": price, "product": product, "trade_date": "2026-11-03",
            "timestamp": "2026-11-03 10:15:00"}


def test_reconcile_partial_then_full_ignores_mis_flags_unplanned(book, settings):
    pid = _proposal(book, settings, qty=5)
    proposal.approve(book, pid, settings, channel="cli")
    ev = reconcile.from_trades(book, [kt("t1", "SELL", 3), kt("m1", "BUY", 50, "MIS"), kt("m2", "SELL", 50, "MIS")])
    assert ev == [f"proposal #{pid} partly filled: 3 of 5 ACME"]
    ev = reconcile.from_trades(book, [kt("t1", "SELL", 3), kt("t2", "SELL", 2), kt("t3", "BUY", 1, symbol="NIFTYETF")])
    assert ev == [f"proposal #{pid} reconciled (SELL 5 ACME)", "UNPLANNED BUY 1 NIFTYETF @ 1231.0"]
    assert book.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()[0] == "reconciled"
    assert book.execute("SELECT COUNT(*) FROM journal WHERE kind='unplanned'").fetchone()[0] == 1
    # MIS trades are stored (for tax netting) but never filled
    assert book.execute("SELECT COUNT(*) FROM trades WHERE product='MIS'").fetchone()[0] == 2
    assert book.execute("SELECT COUNT(*) FROM fills WHERE trade_id LIKE 'm%'").fetchone()[0] == 0


def test_reconcile_from_holdings_when_no_trades_seen(book, settings):
    pid = _proposal(book, settings, qty=5)
    proposal.approve(book, pid, settings, channel="cli")
    repo.replace_holdings(book, [Holding("ACME", "NSE", "INE000A01011", 6, 500.0)], {}, "2026-11-04T18:00:00+05:30")
    assert reconcile.from_holdings(book) == [f"proposal #{pid} partly filled: 4 of 5 ACME"]
    assert reconcile.from_holdings(book) == []        # idempotent
    f = book.execute("SELECT source, price FROM fills").fetchone()
    assert tuple(f) == ("holdings_diff", 450.6)


class FakeKite:
    def __init__(self, trades, holdings, cash=2500.0):
        self._t, self._h, self._c = trades, holdings, cash

    def cash(self):
        return self._c, {"net": self._c}

    def trades(self):
        return self._t

    def holdings(self):
        return self._h


def test_sync_end_to_end(book, settings):
    pid = _proposal(book, settings, qty=10, reason="kill-criterion:profit_falls")
    proposal.approve(book, pid, settings, channel="dashboard")
    out = reconcile.sync(book, settings, FakeKite([kt("t9", "SELL", 10)], []))   # sold out
    assert out["events"] == [f"proposal #{pid} reconciled (SELL 10 ACME)"]
    assert out["sold_out"] == ["ACME", "LIQ", "NIFTYETF"]
    assert portfolio.kite_cash(book)[0] == 2500.0


def test_kite_client_parses_trades_and_cash():
    class R:
        def __init__(self, body): self.status_code, self._b = 200, body
        def json(self): return self._b

    class H:
        def request(self, method, url, **kw):
            if url.endswith("/trades"):
                return R({"status": "success", "data": [{"trade_id": 101, "order_id": 9, "tradingsymbol": "infy",
                          "exchange": "NSE", "transaction_type": "SELL", "quantity": 3, "average_price": 1500.5,
                          "product": "CNC", "fill_timestamp": "2026-11-03 10:15:00"}]})
            return R({"status": "success", "data": {"equity": {"net": 1234.5}}})

    c = kite.KiteClient("k", "s", access_token="t", http=H())
    [t] = c.trades()
    assert (t["trade_id"], t["symbol"], t["side"], t["trade_date"], t["product"]) == ("101", "INFY", "SELL", "2026-11-03", "CNC")
    assert c.cash()[0] == 1234.5
