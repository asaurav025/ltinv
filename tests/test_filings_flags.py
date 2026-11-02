"""Phase 1: feeds -> filings -> red flags -> resolve (guardrails) -> proposals -> digest."""

import gzip
import json
from datetime import date

import pytest

from ltinv import digest, pipeline, refdata
from ltinv.actions import flags as flags_mod
from ltinv.actions import proposal
from ltinv.db import repo
from ltinv.db.repo import Holding
from ltinv.fetch import announcements, bhavcopy
from ltinv.rules import redflags
from tests.conftest import FIXTURES

NSE_XML = (FIXTURES / "nse_rss_sample.xml").read_bytes()
BSE_XML = (FIXTURES / "bse_rss_sample.xml").read_bytes()
# Real NSE EQUITY_L rows (public); OM POWER and Narmada are in the RSS sample.
EQUITY_L = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
    "OMPOWER,Om Power Transmission Limited,EQ,01-JAN-2025,10,1,INE000OMP011,10\n"
    "NARMADA,Narmada Agrobase Limited,EQ,01-JAN-2020,10,1,INE000NAR011,10\n"
    "UNIVCABLES,Universal Cables Limited,EQ,17-MAY-1995,10,1,INE279A01012,10\n"
).encode()
RULES = redflags.load_rules({"rules": [
    {"id": "auditor_resignation", "severity": "fatal", "detect": "subject", "text": "Statutory auditor resigns"},
    {"id": "kmp_exit_no_successor", "severity": "serious", "detect": "llm", "text": "CFO/MD exit, no successor"},
    {"id": "independent_director_governance_exit", "severity": "serious", "detect": "llm", "text": "ID exit"},
    {"id": "pledge_invocation", "severity": "fatal", "detect": "subject", "text": "Pledge invoked"},
    {"id": "acquisition_outside_core", "severity": "watch", "detect": "llm", "text": "Acquisition"},
    {"id": "price_volume_drop", "severity": "watch", "detect": "data", "text": "Price drop on volume"},
]})


class Resp:
    def __init__(self, status, content):
        self.status_code, self.content = status, content


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for key, body in self.routes.items():
            if key in url:
                return Resp(200, body) if body is not None else Resp(500, b"")
        return Resp(404, b"")


def http(**extra):
    routes = {"Online_announcements": NSE_XML, "announcements.xml": BSE_XML, "EQUITY_L": EQUITY_L}
    routes.update(extra)
    return FakeHTTP(routes)


def hold(conn, *rows):
    repo.replace_holdings(conn, [Holding(*r) for r in rows], {}, "2026-11-02T18:00:00+05:30")


# --- feeds ------------------------------------------------------------------------------


def test_parse_nse_real_sample():
    items = announcements.parse_nse(NSE_XML)
    by = {a.company: a for a in items}
    om = [a for a in items if a.company == "Om Power Transmission Limited"]
    assert {a.category for a in om} == {"Resignation of Director/KMP/SMP", "Change in Directors/KMP/SMP/Auditor/RTA"}
    tn = by["Tamilnadu PetroProducts Limited"]
    assert tn.url == "https://nsearchives.nseindia.com/corporate/TPLSECTL_02112026194814_sdOutcomeofBoardMeeting30092026.PDF"
    assert tn.filed_at.endswith("+05:30")
    assert by["DSP NIFTY 1D Rate Liquid ETF"].url is None  # generic listing page dropped
    assert len({a.uid for a in items}) == len(items)


def test_parse_bse_real_sample():
    items = announcements.parse_bse(BSE_XML)
    om = next(a for a in items if a.scripcode == "544750")
    assert om.company == "Om Power Transmission Ltd"
    assert om.subject == "Intimation of resignation of Chief Financial Officer of the Company"
    assert om.url.startswith("https://www.bseindia.com/xml-data/corpfiling/AttachLive/")


def test_feed_errors():
    with pytest.raises(announcements.FeedError):
        announcements.parse_nse(b"<html>blocked</html")


@pytest.mark.parametrize("a,b", [
    ("Universal Cables Limited", "UNIVERSAL CABLES LTD."),
    ("Om Power Transmission Ltd", "Om Power Transmission Limited"),
    ("Tata Consultancy Services & Co.", "tata consultancy services and"),
])
def test_norm_name(a, b):
    assert refdata.norm_name(a) == refdata.norm_name(b)


def test_collect_matches_by_name_and_skips_untracked(conn, tmp_path):
    hold(conn, ("OMPOWER", "NSE", "INE000OMP011", 10, 100.0), ("INDEXETF", "NSE", "INF000X01011", 5, 1.0))
    h = http()
    s = announcements.collect(conn, tmp_path, date(2026, 11, 2), h)
    assert s.new == {"OMPOWER": 2} and s.errors == [] and s.not_covered == []
    assert not any("bseindia" in u for u in h.calls)  # no BSE-only holding -> BSE feed not fetched
    again = announcements.collect(conn, tmp_path, date(2026, 11, 2), h)
    assert again.new == {}  # stored once


def test_collect_uses_bse_for_bse_only_listing(conn, tmp_path):
    hold(conn, ("OMBSE", "BSE", "INE999Z01011", 3, 50.0))  # not in EQUITY_L -> BSE-only
    f = bhavcopy.cache_path(tmp_path, date(2026, 10, 30), bhavcopy.BSE)
    f.parent.mkdir(parents=True)
    f.write_bytes(gzip.compress(b"TradDt,FinInstrmId,ISIN,TckrSymb,SctySrs,ClsPric\n"
                                b"2026-10-30,544750,INE999Z01011,OMBSE,A,10\n"))
    s = announcements.collect(conn, tmp_path, date(2026, 11, 2), http())
    assert s.new == {"OMBSE": 1}
    row = conn.execute("SELECT * FROM filings").fetchone()
    assert row["exchange"] == "BSE" and "Chief Financial Officer" in row["subject"]


def test_collect_reports_unmapped_and_feed_errors(conn, tmp_path):
    hold(conn, ("GHOST", "BSE", "INE888Z01011", 1, 1.0))  # BSE-only, no bhavcopy cached
    s = announcements.collect(conn, tmp_path, date(2026, 11, 2), http(Online_announcements=None))
    assert s.not_covered == ["GHOST"]
    assert s.errors and "NSE feed" in s.errors[0]


# --- rules ------------------------------------------------------------------------------


@pytest.mark.parametrize("category,subject,expected", [
    (None, "Resignation of Statutory Auditors of the Company", ["auditor_resignation"]),
    (None, "Resignation of Internal Auditor", []),
    (None, "Intimation of resignation of Chief Financial Officer of the Company", ["kmp_exit_no_successor"]),
    ("Change in Directors/KMP/SMP/Auditor/RTA", "X has informed the Exchange about Change in Directors/KMP/SMP/Auditor/RTA",
     ["governance_change"]),
    ("Resignation of Director/KMP/SMP", "X has informed the Exchange about Resignation of Director/KMP/SMP",
     ["governance_change"]),
    (None, "Invocation of pledge on promoter shares by lender", ["pledge_invocation"]),
    ("Outcome of Board Meeting", "financial results for the period ended September 30, 2026", []),
])
def test_evaluate(category, subject, expected):
    assert [r.id for r, _ in redflags.evaluate(RULES, category, subject)] == expected


def test_governance_change_is_built_in_and_can_be_disabled():
    assert RULES["governance_change"].reclassify_to[0] == "auditor_resignation"
    off = redflags.load_rules({"rules": [{"id": "governance_change", "enabled": False}]})
    assert "governance_change" not in off


def test_bad_severity_in_config():
    with pytest.raises(ValueError):
        redflags.load_rules({"rules": [{"id": "x", "severity": "catastrophic"}]})


def _thesis(conn, symbol, kills):
    repo.upsert_thesis(conn, symbol, f"thesis/{symbol}.md", 3, kills, [], "2026-11-02T00:00:00+05:30")


def _filing(conn, symbol, subject, category=None, uid="u1"):
    announcements.store(conn, symbol, announcements.Announcement(
        "NSE", symbol + " Limited", None, category, subject, "https://nsearchives.nseindia.com/corporate/x.pdf",
        "2026-11-02T19:00:00+05:30", uid))
    conn.commit()


def test_scan_links_thesis_kill_criterion_and_runs_once(conn):
    hold(conn, ("ACME", "NSE", "INE000A01011", 10, 100.0))
    _thesis(conn, "ACME", [{"id": "auditor_quits", "rule": "auditor_resignation", "text": ""}])
    _filing(conn, "ACME", "Resignation of Statutory Auditor with immediate effect")
    new = redflags.scan_filings(conn, RULES)
    assert len(new) == 1 and redflags.scan_filings(conn, RULES) == []
    f = conn.execute("SELECT * FROM flags").fetchone()
    assert (f["rule_id"], f["severity"], f["kill_criterion"], f["llm_confirmed"]) == \
        ("auditor_resignation", "fatal", "auditor_quits", None)


def test_price_volume_drop(conn):
    rows = [{"symbol": "ACME", "date": f"2026-10-{d:02d}", "o": 1, "h": 1, "l": 1, "c": 100.0,
             "volume": 1000, "source": "bhavcopy"} for d in range(1, 23)]
    rows.append({"symbol": "ACME", "date": "2026-10-23", "o": 1, "h": 1, "l": 1, "c": 88.0,
                 "volume": 3500, "source": "bhavcopy"})
    repo.upsert_prices(conn, rows)
    assert redflags.price_volume_drops(conn, ["ACME"]) == [
        {"symbol": "ACME", "date": "2026-10-23", "change_pct": -12.0, "volume_mult": 3.5}]
    assert len(redflags.scan_prices(conn, RULES, ["ACME"])) == 1
    assert redflags.scan_prices(conn, RULES, ["ACME"]) == []  # once per day


# --- resolve guardrails -----------------------------------------------------------------


@pytest.fixture
def flagged(conn):
    hold(conn, ("ACME", "NSE", "INE000A01011", 10, 100.0))
    _thesis(conn, "ACME", [{"id": "auditor_quits", "rule": "auditor_resignation", "text": ""}])
    _filing(conn, "ACME", "about Change in Directors/KMP/SMP/Auditor/RTA", "Change in Directors/KMP/SMP/Auditor/RTA")
    repo.upsert_prices(conn, [{"symbol": "ACME", "date": "2026-11-02", "o": 1, "h": 1, "l": 1, "c": 200.0,
                               "volume": 1, "source": "bhavcopy"}])
    [fid] = redflags.scan_filings(conn, RULES)
    return fid


@pytest.mark.parametrize("kwargs,needle", [
    (dict(confirmed=True, rationale="x"), "needs --quote"),
    (dict(confirmed=True, rationale="x", quote="q", rule_id="pledge_invocation"), "can be resolved only as"),
    (dict(confirmed=True, rationale="x", quote="q", severity="serious"), "above the governance_change ceiling"),
    (dict(confirmed=False, rationale="  "), "give a rationale"),
])
def test_resolve_guardrails(conn, flagged, kwargs, needle):
    with pytest.raises(flags_mod.ResolveError, match=needle.replace("(", r"\(")):
        flags_mod.resolve(conn, flagged, RULES, **kwargs)


def test_reclassify_to_kill_criterion_makes_full_sell_proposal(conn, flagged):
    out = flags_mod.resolve(conn, flagged, RULES, confirmed=True, rule_id="auditor_resignation",
                            rationale="The statutory auditor resigned mid-term citing non-cooperation.",
                            quote="M/s ABC & Co. have resigned as Statutory Auditors", by="claude-code")
    assert out == {"flag_id": flagged, "confirmed": True, "rule_id": "auditor_resignation",
                   "severity": "fatal", "kill_criterion": "auditor_quits"}
    [pid] = proposal.from_flags(conn)
    p = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    assert (p["side"], p["qty"], p["reason"], p["limit_low"], p["limit_high"]) == \
        ("SELL", 10, "kill-criterion:auditor_quits", 194.0, 206.0)
    assert json.loads(p["evidence_json"])["rule"] == "auditor_resignation"
    assert proposal.from_flags(conn) == []  # deduped
    with pytest.raises(flags_mod.ResolveError, match="already resolved"):
        flags_mod.resolve(conn, flagged, RULES, confirmed=False, rationale="again")


def test_lowered_severity_and_dismissal_make_no_proposal(conn, flagged):
    flags_mod.resolve(conn, flagged, RULES, confirmed=True, rule_id="kmp_exit_no_successor",
                      severity="watch", rationale="CFO resigned; successor named.", quote="Mr. X appointed as CFO")
    assert proposal.from_flags(conn) == []
    f = conn.execute("SELECT status, severity FROM flags WHERE id=?", (flagged,)).fetchone()
    assert tuple(f) == ("open", "watch")


def test_serious_confirmed_trims_half(conn):
    hold(conn, ("ACME", "NSE", "INE000A01011", 9, 100.0))
    _filing(conn, "ACME", "Intimation of resignation of Chief Financial Officer")
    [fid] = redflags.scan_filings(conn, RULES)
    flags_mod.resolve(conn, fid, RULES, confirmed=True, rationale="CFO left, no successor.", quote="CFO resigned")
    [pid] = proposal.from_flags(conn)
    p = conn.execute("SELECT qty, reason FROM proposals WHERE id=?", (pid,)).fetchone()
    assert tuple(p) == (4, "red-flag:kmp_exit_no_successor")


def test_proposal_rules():
    import sqlite3
    with pytest.raises(proposal.ProposalError):
        proposal.create(sqlite3.connect(":memory:"), "X", "HOLD", 1, "r", {"a": 1})


def test_expire(conn):
    pid, _ = proposal.create(conn, "ACME", "SELL", 1, "manual", {"path": "x"}, ttl_hours=-1)
    assert proposal.expire(conn) == [pid]


# --- digest & pipeline ------------------------------------------------------------------


def test_digest_lists_unjudged_flag_and_proposal(conn, flagged):
    md, attention = digest.daily(conn, date.today())
    assert "governance_change" in md and "not judged yet" in md
    assert "**thesis kill criterion" not in md  # governance_change itself is not a kill rule
    proposal.create(conn, "ACME", "SELL", 1, "manual", {"path": "x"})
    md, attention = digest.daily(conn, date.today())
    assert attention and "Open proposals (1)" in md


def test_run_daily_end_to_end_offline(conn, settings, monkeypatch):
    hold(conn, ("OMPOWER", "NSE", "INE000OMP011", 10, 100.0))
    monkeypatch.setattr(bhavcopy, "fetch_prices", lambda *a, **k: bhavcopy.FetchSummary(rows_written=0))
    report = pipeline.run_daily(conn, settings, http())
    got = {s.step: s.status for s in report.steps}
    assert got == {"holdings": "skipped", "prices": "ok", "filings": "ok", "scan": "ok",
                   "proposals": "ok", "digest": "ok", "push": "skipped"}, report.steps
    assert conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0] == 2  # two OM POWER governance items
    assert list((settings.data_dir / "digests").rglob("daily-*.md"))
