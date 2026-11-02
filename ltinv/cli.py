"""ltinv command line. Cron, the dashboard and Claude skills all go through these commands."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

import typer

from ltinv import __version__
from ltinv import thesis as thesis_mod
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
app.add_typer(kite_app, name="kite")
app.add_typer(fetch_app, name="fetch")
app.add_typer(thesis_app, name="thesis")

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


if __name__ == "__main__":
    app()
