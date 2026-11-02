"""ltinv command line. Cron, the dashboard and Claude skills all go through these commands."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import date
from pathlib import Path
from typing import Optional

import typer

from ltinv import __version__, digest, pipeline
from ltinv import thesis as thesis_mod
from ltinv.actions import flags as flags_mod
from ltinv.actions import proposal
from ltinv.rules import redflags
from ltinv.config import CONFIG_FILES, ConfigError, Settings, get_settings, write_missing_config
from ltinv.db import repo
from ltinv.fetch import bhavcopy
from ltinv.fetch import kite as kite_mod
from ltinv.instruments import ORDER, needs_thesis
from ltinv.timeutil import iso, now_ist, today_ist

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Long-term investor suite: theses, red flags, reviews, approvals.")
kite_app = typer.Typer(no_args_is_help=True, help="Kite session (one manual login per trading day).")
fetch_app = typer.Typer(no_args_is_help=True, help="Pull data into the DB.")
thesis_app = typer.Typer(no_args_is_help=True, help="Written investment theses.")
flags_app = typer.Typer(no_args_is_help=True, help="Red flags: scan filings, list, judge.")
proposal_app = typer.Typer(no_args_is_help=True, help="Trade proposals (never orders).")
digest_app = typer.Typer(no_args_is_help=True, help="Digests of what needs attention.")
run_app = typer.Typer(no_args_is_help=True, help="Pipelines cron runs.")
journal_app = typer.Typer(no_args_is_help=True, help="Decision journal.")
app.add_typer(kite_app, name="kite")
app.add_typer(fetch_app, name="fetch")
app.add_typer(thesis_app, name="thesis")
app.add_typer(flags_app, name="flags")
app.add_typer(proposal_app, name="proposal")
app.add_typer(digest_app, name="digest")
app.add_typer(run_app, name="run")
app.add_typer(journal_app, name="journal")

DATA_GITIGNORE = "ltinv.db\nltinv.db-*\ncache/\n"


class State:
    home: Optional[Path] = None


def settings() -> Settings:
    return get_settings(State.home)


def open_db(s: Settings):
    if not s.db_path.exists():
        fail(f"no database at {s.db_path}; run `ltinv init` first")
    conn = repo.connect(s.db_path)
    if repo.schema_version(conn) < max(n for n, _, _ in repo.migrations()):
        fail("database schema is behind; run `ltinv init` to migrate")
    return conn


def fail(msg: str, code: int = 1) -> None:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


@app.callback()
def main(
    home: Optional[Path] = typer.Option(None, "--home", envvar="LTINV_HOME",
                                        help="Folder holding config/ and data/ (default: cwd)."),
    version: bool = typer.Option(False, "--version", is_eager=True, help="Print the version."),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    State.home = home


# --- init / status ----------------------------------------------------------------------


@app.command()
def init(git: bool = typer.Option(True, help="Make data/ its own git repo.")) -> None:
    """Create config/ and data/, apply DB migrations. Safe to re-run."""
    s = settings()
    created = write_missing_config(s)
    if created:
        typer.echo(f"config    {s.config_dir}: created {', '.join(created)} from examples; review them")
    for name in CONFIG_FILES:
        try:
            s.load(name)
        except ConfigError as exc:
            fail(str(exc))
    for sub in ("thesis", "reviews", "journal", "raw", "cache"):
        (s.data_dir / sub).mkdir(parents=True, exist_ok=True)
    gi = s.data_dir / ".gitignore"
    if not gi.exists():
        gi.write_text(DATA_GITIGNORE)
    conn = repo.connect(s.db_path)
    applied = repo.migrate(conn)
    typer.echo(f"database  {s.db_path} (schema v{repo.schema_version(conn)}"
               + (f", applied {', '.join(applied)})" if applied else ", up to date)"))
    if git and not (s.data_dir / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=s.data_dir, check=True)
        typer.echo(f"data repo {s.data_dir} (git init; add a private remote to back it up)")
    if not s.env("KITE_API_KEY"):
        typer.echo("note      KITE_API_KEY not set; copy .env.example to .env and fill it")


@app.command()
def status() -> None:
    """DB, Kite session, holdings, prices and theses at a glance."""
    s = settings()
    conn = open_db(s)
    hold = repo.holdings(conn)
    sectors = s.load("sectors")
    equities = [h["symbol"] for h in hold if needs_thesis(h["asset_class"])]
    no_sector = [sym for sym in equities if not sectors.get(sym)]
    last_price = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    no_price = [r[0] for r in conn.execute(
        "SELECT symbol FROM holdings WHERE symbol NOT IN (SELECT DISTINCT symbol FROM prices)")]
    theses = {r["symbol"] for r in conn.execute("SELECT symbol FROM theses")}
    open_props = conn.execute("SELECT COUNT(*) FROM proposals WHERE status='open'").fetchone()[0]
    by_class: dict[str, int] = {}
    for h in hold:
        by_class[h["asset_class"] or "unclassified"] = by_class.get(h["asset_class"] or "unclassified", 0) + 1
    breakdown = ", ".join(f"{by_class[c]} {c}" for c in (*ORDER, "unclassified") if c in by_class)
    typer.echo(f"database   {s.db_path} (schema v{repo.schema_version(conn)})")
    typer.echo(f"kite       {kite_mod.session_status(conn)}")
    typer.echo(f"holdings   {len(hold)}" + (f" ({breakdown}; as of {hold[0]['as_of']})" if hold else ""))
    if "unclassified" in by_class:
        typer.echo("           run `ltinv fetch holdings` to classify them")
    typer.echo(f"prices     last close {last_price or 'none'}"
               + (f"; none for: {', '.join(no_price)}" if no_price else ""))
    typer.echo(f"theses     {len(theses)} validated; equities without one: "
               f"{', '.join(sorted(set(equities) - theses)) or 'none'}")
    if no_sector:
        typer.echo(f"sectors    missing in config/sectors.yaml: {', '.join(no_sector)}")
    typer.echo(f"proposals  {open_props} open")
    for r in conn.execute(
        """SELECT entry, step, status, finished_at, detail FROM runs
           WHERE id IN (SELECT MAX(id) FROM runs GROUP BY entry, step) ORDER BY entry, step"""
    ):
        typer.echo(f"run        {r['entry']}/{r['step']}: {r['status']} {r['finished_at'] or ''}"
                   + (f" ({r['detail']})" if r["detail"] else ""))


# --- kite -------------------------------------------------------------------------------


@kite_app.command("login")
def kite_login(
    url: Optional[str] = typer.Option(None, "--url",
                                      help="The URL Kite redirected you to (or the request_token)."),
) -> None:
    """Print the Kite login link, then store today's session from the redirect URL."""
    s = settings()
    conn = open_db(s)
    try:
        client = kite_mod.KiteClient.from_settings(s)
        if url is None:
            typer.echo("1. Open this link and log in to Kite:\n")
            typer.echo(f"   {client.login_url()}\n")
            typer.echo("2. Kite sends you to your app's redirect URL. Copy that whole URL "
                       "from the address bar (the page itself may not load).")
            url = typer.prompt("\nPaste it here", hide_input=True)
        result = kite_mod.login(s, conn, url, client=client)
    except (ConfigError, kite_mod.KiteError) as exc:
        fail(str(exc))
    typer.echo(f"Kite session stored for {result['user_id'] or '?'}, valid until {result['expires_at']}.")


# --- fetch ------------------------------------------------------------------------------


@fetch_app.command("holdings")
def fetch_holdings() -> None:
    """Snapshot Kite holdings into the DB. Skipped (exit 0) without today's Kite session."""
    s = settings()
    conn = open_db(s)
    with repo.run_step(conn, "fetch", "holdings") as info:
        token = kite_mod.active_token(conn)
        if token is None:
            info.update(status="skipped", detail="no active Kite session")
            typer.echo("skipped: no active Kite session today; run `ltinv kite login`")
            return
        try:
            client = kite_mod.KiteClient.from_settings(s, access_token=token)
            rows = client.holdings()
        except kite_mod.KiteAuthError as exc:
            info.update(status="skipped", detail=str(exc))
            typer.echo(f"skipped: {exc}; run `ltinv kite login`")
            return
        except (ConfigError, kite_mod.KiteError) as exc:
            fail(str(exc))
        n, gone = repo.replace_holdings(conn, rows, s.load("sectors"), iso(now_ist()),
                                        cash_like=s.load("risk").get("cash_like_symbols") or [])
        info["detail"] = f"{n} holdings"
    typer.echo(f"holdings: {n} written" + (f"; no longer held: {', '.join(gone)}" if gone else ""))


@fetch_app.command("prices")
def fetch_prices(
    symbols: Optional[list[str]] = typer.Argument(None, help="Symbols (default: holdings + theses)."),
    range_: str = typer.Option("10d", "--range", help="How far back: 30d, 6m, 1y ..."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Daily closes from NSE bhavcopy (cached, so a 1y back-fill downloads once)."""
    s = settings()
    conn = open_db(s)
    targets: dict[str, Optional[str]] = {}
    if symbols:
        known = {h["symbol"]: h["isin"] for h in repo.holdings(conn)}
        for raw in symbols:
            sym = thesis_mod.normalise_symbol(raw)
            targets[sym] = known.get(sym)
    else:
        targets = {h["symbol"]: h["isin"] for h in repo.holdings(conn)}
        for r in conn.execute("SELECT symbol FROM theses"):
            targets.setdefault(r["symbol"], None)
    if not targets:
        fail("no symbols: run `ltinv fetch holdings`, add a thesis, or pass symbols")
    try:
        span = bhavcopy.parse_range(range_)
    except bhavcopy.BhavcopyError as exc:
        fail(str(exc))
    today = today_ist()
    with repo.run_step(conn, "fetch", "prices") as info:
        summary = bhavcopy.fetch_prices(
            conn, s.cache_dir, targets, bhavcopy.days_for(today, span), today,
            progress=None if quiet else (lambda m: typer.echo(f"  {m}")),
        )
        info["detail"] = (f"{summary.rows_written} rows, latest {summary.latest_date}, "
                          f"{summary.downloaded} downloaded")
    typer.echo(f"prices: {summary.rows_written} rows over {summary.days_with_file} trading days "
               f"({summary.downloaded} files downloaded), latest close {summary.latest_date or 'none'}")
    if summary.from_bse:
        typer.echo(f"from BSE (not on NSE): {', '.join(sorted(summary.from_bse))}")
    if summary.unmatched:
        typer.secho(f"no bhavcopy rows for: {', '.join(sorted(summary.unmatched))} "
                    "(not NSE-listed, renamed, or a typo?)", fg=typer.colors.YELLOW)


# --- thesis -----------------------------------------------------------------------------


def _validate(s: Settings, symbol: str) -> thesis_mod.Validation:
    path = thesis_mod.path_for(s.thesis_dir, symbol)
    if not path.exists():
        fail(f"no thesis at {path}; run `ltinv thesis new {symbol}`")
    return thesis_mod.validate(
        path.read_text(), symbol,
        rule_ids=thesis_mod.rule_ids(s.load("red-flags")),
        single_stock_max_pct=s.load("risk").get("single_stock_max_pct"),
    )


def _report_and_store(s: Settings, v: thesis_mod.Validation) -> None:
    for w in v.warnings:
        typer.secho(f"  warning: {w}", fg=typer.colors.YELLOW)
    if not v.ok:
        for e in v.errors:
            typer.secho(f"  error: {e}", fg=typer.colors.RED)
        fail(f"{v.symbol}: thesis not valid ({len(v.errors)} problem(s)); not saved to the DB")
    conn = open_db(s)
    path = thesis_mod.path_for(s.thesis_dir, v.symbol)
    repo.upsert_thesis(conn, v.symbol, str(path.relative_to(s.data_dir)), v.target_weight_pct,
                       v.kill_criteria, v.metrics, iso(now_ist()))
    typer.echo(f"{v.symbol}: valid — {len(v.metrics)} metric(s), "
               f"{len(v.kill_criteria)} kill criteria, target {v.target_weight_pct}%")


@thesis_app.command("new")
def thesis_new(symbol: str) -> None:
    """Scaffold data/thesis/SYMBOL.md from the template."""
    s = settings()
    open_db(s)
    try:
        path = thesis_mod.create(s.thesis_dir, symbol, today_ist().isoformat())
    except thesis_mod.ThesisError as exc:
        fail(str(exc))
    typer.echo(f"created {path}\nfill it in, then run `ltinv thesis validate {symbol.upper()}`")


@thesis_app.command("show")
def thesis_show(symbol: str) -> None:
    """Print the thesis and whether it validates."""
    s = settings()
    v = _validate(s, symbol)
    typer.echo(thesis_mod.path_for(s.thesis_dir, symbol).read_text())
    typer.echo("status: " + ("valid" if v.ok else f"NOT valid ({len(v.errors)} problem(s))"))


@thesis_app.command("edit")
def thesis_edit(symbol: str) -> None:
    """Open the thesis in $EDITOR, then validate it."""
    s = settings()
    path = thesis_mod.path_for(s.thesis_dir, symbol)
    if not path.exists():
        fail(f"no thesis at {path}; run `ltinv thesis new {symbol}`")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.run([*shlex.split(editor), str(path)], check=False)
    _report_and_store(s, _validate(s, symbol))


@thesis_app.command("validate")
def thesis_validate(symbol: str) -> None:
    """Check forced sections and machine-checkable kill criteria; store in DB if valid."""
    s = settings()
    try:
        v = _validate(s, symbol)
    except thesis_mod.ThesisError as exc:
        fail(str(exc))
    _report_and_store(s, v)


# --- filings & flags ----------------------------------------------------------------------


@fetch_app.command("filings")
def fetch_filings(quiet: bool = typer.Option(False, "--quiet", "-q")) -> None:
    """Store today's NSE/BSE announcements for your stocks, then run the red-flag rules.
    The feeds hold only today, so cron runs this hourly."""
    s = settings()
    conn = open_db(s)
    try:
        with repo.run_step(conn, "fetch", "filings") as info:
            _, info["detail"] = pipeline.collect_filings(conn, s)
        scanned = pipeline.scan(conn, s)
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
    if not quiet:
        typer.echo(f"filings: {info['detail']}\nscan: {scanned}")


def _rules(s: Settings):
    try:
        return redflags.load_rules(s.load("red-flags"))
    except (ConfigError, ValueError) as exc:
        fail(str(exc))


@flags_app.command("scan")
def flags_scan() -> None:
    """Run the red-flag rules over filings not yet scanned, and over the latest prices."""
    s = settings()
    conn = open_db(s)
    typer.echo(pipeline.scan(conn, s))


@flags_app.command("list")
def flags_list(
    status: Optional[str] = typer.Option("open", help="open | dismissed | all"),
    pending: bool = typer.Option(False, "--pending", help="Only flags nobody has judged yet."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List red flags, most severe first."""
    conn = open_db(settings())
    if pending:
        rows = flags_mod.pending(conn)
    else:
        where = "" if status == "all" else "WHERE f.status=?"
        rows = conn.execute(
            f"""SELECT f.*, fl.company, fl.subject AS filing_subject, fl.url FROM flags f
                LEFT JOIN filings fl ON fl.id=f.filing_id {where}
                ORDER BY CASE f.severity WHEN 'fatal' THEN 0 WHEN 'serious' THEN 1 ELSE 2 END, f.id""",
            () if status == "all" else (status,)).fetchall()
    if as_json:
        typer.echo(json.dumps([dict(r) for r in rows], indent=2))
        return
    if not rows:
        typer.echo("no flags")
    for r in rows:
        state = {1: "confirmed", 0: "dismissed"}.get(r["llm_confirmed"], "not judged")
        kill = f" KILL:{r['kill_criterion']}" if r["kill_criterion"] else ""
        typer.echo(f"#{r['id']:<4} {r['severity']:<7} {r['symbol']:<12} {r['rule_id']:<36} {state}{kill}"
                   f"  {(r['filing_subject'] or r['detail'] or '')[:80]}")


@flags_app.command("show")
def flags_show(flag_id: int, as_json: bool = typer.Option(False, "--json")) -> None:
    """A flag, its filing (with document link), and the rules it may be resolved as."""
    s = settings()
    conn = open_db(s)
    try:
        d = flags_mod.describe(conn, flag_id, _rules(s))
    except flags_mod.ResolveError as exc:
        fail(str(exc))
    if as_json:
        typer.echo(json.dumps(d, indent=2))
        return
    f, fl = d["flag"], d["filing"]
    typer.echo(f"flag #{f['id']} {f['severity']} {f['symbol']} {f['rule_id']} ({f['status']})")
    typer.echo(f"matched: {f['detail']}")
    if fl:
        typer.echo(f"filing:  {fl['company']} on {fl['exchange']} at {fl['filed_at']}")
        typer.echo(f"         {fl['category'] or ''} {fl['subject']}")
        typer.echo(f"doc:     {fl['url'] or '(no document)'}")
    typer.echo("may be resolved as: " + ", ".join(f"{o['rule_id']} (≤{o['severity_ceiling']})" for o in d["options"]))
    if f["llm_rationale"]:
        typer.echo(f"verdict: {f['llm_rationale']}")


@flags_app.command("resolve")
def flags_resolve(
    flag_id: int,
    confirm: bool = typer.Option(..., "--confirm/--dismiss", help="Is the red flag real?"),
    rationale: str = typer.Option(..., "--rationale", help="What happened, and why it is or is not the flag."),
    quote: str = typer.Option("", "--quote", help="Sentence copied exactly from the filing (required to confirm)."),
    rule: Optional[str] = typer.Option(None, "--rule", help="Reclassify to one of the allowed rules."),
    severity: Optional[str] = typer.Option(None, "--severity", help="Keep or lower the rule's severity."),
    by: str = typer.Option("manual", "--by", help="Who judged: manual | claude-code."),
) -> None:
    """Record a verdict on a flag. Confirmed fatal/serious flags and thesis kill criteria
    become SELL proposals right away."""
    s = settings()
    conn = open_db(s)
    try:
        out = flags_mod.resolve(conn, flag_id, _rules(s), confirmed=confirm, rationale=rationale,
                                quote=quote, rule_id=rule, severity=severity, by=by)
    except flags_mod.ResolveError as exc:
        fail(str(exc))
    typer.echo(f"flag #{flag_id}: {'confirmed' if out['confirmed'] else 'dismissed'} as {out['rule_id']} "
               f"({out['severity']})" + (f", thesis kill criterion {out['kill_criterion']}" if out["kill_criterion"] else ""))
    for pid in proposal.from_flags(conn):
        p = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        typer.echo(f"proposal #{pid}: {p['side']} {p['qty']} {p['symbol']} — {p['reason']} "
                   f"(expires {p['expires_at']})")


# --- proposals ----------------------------------------------------------------------------


def _print_proposal(p) -> None:
    band = f" limit {p['limit_low']}–{p['limit_high']}" if p["limit_low"] else ""
    typer.echo(f"#{p['id']:<4} {p['status']:<9} {p['side']} {p['qty']} {p['symbol']}{band} — {p['reason']}"
               f" (expires {p['expires_at']})")


@proposal_app.command("list")
def proposal_list(status: str = typer.Option("open", help="open | expired | all ..."),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    conn = open_db(settings())
    rows = proposal.listing(conn, None if status == "all" else status)
    if as_json:
        typer.echo(json.dumps([dict(r) for r in rows], indent=2))
        return
    if not rows:
        typer.echo("no proposals")
    for p in rows:
        _print_proposal(p)


@proposal_app.command("show")
def proposal_show(proposal_id: int) -> None:
    conn = open_db(settings())
    p = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if p is None:
        fail(f"no proposal #{proposal_id}")
    _print_proposal(p)
    typer.echo("evidence: " + json.dumps(json.loads(p["evidence_json"]), indent=2))
    typer.echo("tax/risk checks arrive in Phase 2.")


@proposal_app.command("create")
def proposal_create(
    symbol: str = typer.Option(...), side: str = typer.Option(...), qty: int = typer.Option(...),
    reason: str = typer.Option(...),
    evidence: str = typer.Option(..., help="Path to a review/flag/file under data/ backing this."),
) -> None:
    """A manual proposal. Needs evidence: proposals never come from chat reasoning alone."""
    s = settings()
    conn = open_db(s)
    if not (s.data_dir / evidence).exists() and not Path(evidence).exists():
        fail(f"evidence {evidence} not found")
    try:
        pid, created = proposal.create(conn, thesis_mod.normalise_symbol(symbol), side, qty, reason,
                                       {"path": evidence})
    except (proposal.ProposalError, thesis_mod.ThesisError) as exc:
        fail(str(exc))
    typer.echo(f"proposal #{pid} {'created' if created else 'already open'}")


@proposal_app.command("expire")
def proposal_expire() -> None:
    conn = open_db(settings())
    typer.echo(f"expired: {proposal.expire(conn) or 'none'}")


# --- digest / run / journal -----------------------------------------------------------------


@digest_app.command("daily")
def digest_daily(day: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD (default today)"),
                 write: bool = typer.Option(True, help="Also save it under data/digests/.")) -> None:
    s = settings()
    conn = open_db(s)
    d = date.fromisoformat(day) if day else today_ist()
    md, _ = digest.daily(conn, d)
    if write:
        digest.write(s.data_dir, d, md)
    typer.echo(md)


@run_app.command("daily")
def run_daily() -> None:
    """Holdings (if logged in), prices, filings, red-flag scan, proposals, digest, data push."""
    s = settings()
    conn = open_db(s)
    report = pipeline.run_daily(conn, s)
    for st in report.steps:
        typer.echo(f"{st.step:<10} {st.status:<8} {st.detail or ''}")
    if report.failed:
        raise typer.Exit(1)


@journal_app.command("add")
def journal_add(note: str, kind: str = typer.Option("note", help="note | dissent"),
                symbol: Optional[str] = typer.Option(None)) -> None:
    if kind not in ("note", "dissent"):
        fail("kind must be note or dissent")
    conn = open_db(settings())
    repo.add_journal(conn, kind, note, symbol=thesis_mod.normalise_symbol(symbol) if symbol else None)
    typer.echo("journaled")


if __name__ == "__main__":
    app()
