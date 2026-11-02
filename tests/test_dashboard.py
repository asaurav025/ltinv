import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")

from importlib import resources  # noqa: E402

from ltinv.actions import proposal  # noqa: E402
from ltinv.db import repo  # noqa: E402
from ltinv.db.repo import Holding  # noqa: E402

APP = str(resources.files("ltinv") / "dashboard" / "app.py")


@pytest.fixture
def seeded(conn, settings, monkeypatch):
    repo.replace_holdings(conn, [Holding("ACME", "NSE", "INE000A01011", 10, 500.0),
                                 Holding("NIFTYETF", "NSE", "INF000X01011", 100, 280.0)],
                          {}, "2026-11-02T18:00:00+05:30")
    repo.upsert_prices(conn, [{"symbol": "ACME", "date": "2026-11-02", "o": 1, "h": 1, "l": 1,
                               "c": 450.6, "volume": 1, "source": "bhavcopy"}])
    pid, _ = proposal.create(conn, "ACME", "SELL", 10, "kill-criterion:profit_falls",
                             {"rationale": "Profit fell two quarters running."}, s=settings)
    monkeypatch.setenv("LTINV_HOME", str(settings.home))
    return pid


def _run(page: str | None = None):
    at = st_testing.AppTest.from_file(APP, default_timeout=30)
    at.run()
    if page:
        at.switch_page(page)
        at.run()
    assert not at.exception, at.exception
    return at


def test_overview_renders(seeded):
    at = _run()
    assert [m.label for m in at.metric] == ["Portfolio", "Cash", "Top-5 stocks", "Breaches"]
    assert "never logged in" in " ".join(md.value for md in at.markdown)


def test_proposals_view_only_without_identity(seeded, monkeypatch):
    monkeypatch.delenv("LTINV_DASHBOARD_LOCAL_USER", raising=False)
    at = _run("pages/proposals.py")
    assert any("approvals disabled" in w.value for w in at.warning)
    assert at.button(key=f"ok{seeded}").disabled


def test_approve_from_dashboard(seeded, conn, monkeypatch):
    monkeypatch.setenv("LTINV_DASHBOARD_LOCAL_USER", "me@example.com")
    at = _run("pages/proposals.py")
    at.button(key=f"ok{seeded}").click().run()
    assert not at.exception
    assert any("Now place in Kite: SELL 10 ACME" in s.value for s in at.success)
    a = conn.execute("SELECT channel, approved_by FROM approvals WHERE proposal_id=?", (seeded,)).fetchone()
    assert tuple(a) == ("dashboard", "me@example.com")


def test_reject_needs_note(seeded, monkeypatch):
    monkeypatch.setenv("LTINV_DASHBOARD_LOCAL_USER", "me@example.com")
    at = _run("pages/proposals.py")
    at.button(key=f"no{seeded}").click().run()
    assert any("say why" in e.value for e in at.error)
