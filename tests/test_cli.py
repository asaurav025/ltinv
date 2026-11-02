from typer.testing import CliRunner

from ltinv.cli import app
from tests.test_thesis import GOOD

runner = CliRunner()


def run(home, *args):
    return runner.invoke(app, ["--home", str(home), *args])


def test_commands_need_init_first(home):
    r = run(home, "status")
    assert r.exit_code == 1
    assert "run `ltinv init` first" in r.output


def test_init_status_thesis_flow(home):
    r = run(home, "init", "--no-git")
    assert r.exit_code == 0, r.output
    assert "applied 0001_init.sql" in r.output
    assert (home / "data" / ".gitignore").read_text().startswith("ltinv.db")

    r = run(home, "init", "--no-git")
    assert "up to date" in r.output

    r = run(home, "thesis", "new", "infy")
    assert r.exit_code == 0 and (home / "data" / "thesis" / "INFY.md").exists()

    r = run(home, "thesis", "validate", "INFY")
    assert r.exit_code == 1 and "not saved to the DB" in r.output

    (home / "data" / "thesis" / "INFY.md").write_text(GOOD)
    r = run(home, "thesis", "validate", "infy")
    assert r.exit_code == 0, r.output
    assert "INFY: valid — 2 metric(s), 2 kill criteria, target 8.0%" in r.output

    r = run(home, "status")
    assert r.exit_code == 0
    assert "kite       never logged in" in r.output
    assert "theses     1 validated" in r.output


def test_fetch_holdings_skips_without_session(home):
    run(home, "init", "--no-git")
    r = run(home, "fetch", "holdings")
    assert r.exit_code == 0
    assert "skipped: no active Kite session" in r.output
    assert "fetch/holdings: skipped" in run(home, "status").output


def test_fetch_prices_without_symbols_fails(home):
    run(home, "init", "--no-git")
    r = run(home, "fetch", "prices")
    assert r.exit_code == 1 and "no symbols" in r.output


def test_kite_login_without_key_fails_cleanly(home):
    run(home, "init", "--no-git")
    r = run(home, "kite", "login", "--url", "https://x/cb?status=success&request_token=Abc12345")
    assert r.exit_code == 1 and "KITE_API_KEY is not set" in r.output


def test_init_reports_missing_config(home):
    (home / "config" / "tax.yaml").unlink()
    r = run(home, "init")
    assert r.exit_code == 1 and "tax.yaml" in r.output
