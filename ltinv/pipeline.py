"""`ltinv run daily`: the evening pipeline (Architecture §9), deterministic only.

Each step is recorded in `runs`; a failing step is logged and the rest still run. Judging
flags is not a step here: Claude Code does it on your machine (red-flag-scan skill)."""

from __future__ import annotations

import subprocess
import sqlite3
from dataclasses import dataclass, field

import requests

from ltinv import portfolio
from ltinv.actions import proposal, reconcile
from ltinv.config import Settings
from ltinv.db import repo
from ltinv.fetch import announcements, bhavcopy
from ltinv.fetch import kite as kite_mod
from ltinv import digest, refdata
from ltinv.rules import redflags
from ltinv.timeutil import iso, now_ist, today_ist


@dataclass
class StepResult:
    step: str
    status: str
    detail: str | None


@dataclass
class RunReport:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(s.status == "error" for s in self.steps)


def _step(conn, report: RunReport, name: str, fn) -> None:
    try:
        with repo.run_step(conn, "daily", name) as info:
            detail = fn()
            if isinstance(detail, tuple):
                info["status"], info["detail"] = detail
            else:
                info["detail"] = detail
        report.steps.append(StepResult(name, info["status"], info["detail"]))
    except Exception as exc:  # recorded by run_step; keep going
        report.steps.append(StepResult(name, "error", f"{type(exc).__name__}: {exc}"))


def collect_filings(conn: sqlite3.Connection, s: Settings, http=None) -> tuple[str, str]:
    summary = announcements.collect(conn, s.cache_dir, today_ist(), http)
    new = sum(summary.new.values())
    detail = f"{new} new ({summary.nse_items} NSE, {summary.bse_items} BSE items read)"
    if summary.not_covered:
        detail += f"; no feed for {', '.join(summary.not_covered)}"
    if summary.errors:
        raise RuntimeError("; ".join(summary.errors) + f" ({detail})")
    return "ok", detail


def scan(conn: sqlite3.Connection, s: Settings) -> str:
    rules = redflags.load_rules(s.load("red-flags"))
    from_filings = redflags.scan_filings(conn, rules)
    from_prices = redflags.scan_prices(conn, rules, list(refdata.tracked_equities(conn)))
    return f"{len(from_filings)} flag(s) from filings, {len(from_prices)} from prices"


def push_data(s: Settings) -> tuple[str, str]:
    d = s.data_dir
    if not (d / ".git").exists():
        return "skipped", "data/ is not a git repo"
    git = lambda *a: subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)  # noqa: E731
    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return "ok", "nothing to commit"
    c = git("commit", "-q", "-m", f"ltinv run daily {iso(now_ist())}")
    if c.returncode != 0:
        raise RuntimeError(f"git commit: {c.stderr.strip()}")
    if not git("remote").stdout.strip():
        return "ok", "committed (no remote)"
    p = git("push", "-q")
    if p.returncode != 0:
        raise RuntimeError(f"git push: {p.stderr.strip()}")
    return "ok", "committed and pushed"


def run_daily(conn: sqlite3.Connection, s: Settings, http: requests.Session | None = None) -> RunReport:
    http = http or requests.Session()
    today = today_ist()
    r = RunReport()

    def prices():
        targets = {h["symbol"]: h["isin"] for h in repo.holdings(conn)}
        if not targets:
            return "skipped", "no holdings"
        out = bhavcopy.fetch_prices(conn, s.cache_dir, targets, bhavcopy.days_for(today, 10), today, http)
        return "ok", f"{out.rows_written} rows, latest {out.latest_date}"

    def holdings():
        token = kite_mod.active_token(conn)
        if token is None:
            return "skipped", "no Kite session today"
        client = kite_mod.KiteClient.from_settings(s, access_token=token)
        out = reconcile.sync(conn, s, client)
        detail = f"{out['holdings']} holdings, {out['trades']} trades, cash {out['cash']:,.0f}"
        if out["events"]:
            detail += "; " + "; ".join(out["events"])
        return "ok", detail

    def proposals():
        made = proposal.from_flags(conn, s=s)
        gone = proposal.expire(conn)
        return f"{len(made)} new, {len(gone)} expired"

    def risk():
        r = portfolio.risk_check(conn, s)
        return "ok", f"cash {r['cash_pct']}%, top5 {r['top5_pct']}%; " + ("; ".join(r["breaches"]) or "no breaches")

    def write_digest():
        md, attention = digest.daily(conn, today)
        path = digest.write(s.data_dir, today, md)
        return f"{path.relative_to(s.data_dir)}" + (" (needs attention)" if attention else "")

    _step(conn, r, "holdings", holdings)
    _step(conn, r, "prices", prices)
    _step(conn, r, "filings", lambda: collect_filings(conn, s, http))
    _step(conn, r, "scan", lambda: scan(conn, s))
    _step(conn, r, "proposals", proposals)
    _step(conn, r, "risk", risk)
    _step(conn, r, "digest", write_digest)
    _step(conn, r, "push", lambda: push_data(s))
    return r
