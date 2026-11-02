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


def test_init_creates_missing_config_but_never_overwrites(tmp_path):
    r = run(tmp_path, "init", "--no-git")
    assert r.exit_code == 0, r.output
    assert "created risk.yaml, red-flags.yaml, valuation.yaml, kite.yaml, tax.yaml, sectors.yaml" \
        in r.output  # fresh home: all from examples

    (tmp_path / "config" / "sectors.yaml").write_text("MYSTOCK: Chemicals\n")
    (tmp_path / "config" / "tax.yaml").unlink()
    r = run(tmp_path, "init", "--no-git")
    assert "created tax.yaml from examples" in r.output
    assert (tmp_path / "config" / "sectors.yaml").read_text() == "MYSTOCK: Chemicals\n"


def test_init_rejects_broken_config(home):
    (home / "config" / "risk.yaml").write_text("single_stock_max_pct: [unclosed\n")
    r = run(home, "init", "--no-git")
    assert r.exit_code == 1 and "risk.yaml" in r.output
