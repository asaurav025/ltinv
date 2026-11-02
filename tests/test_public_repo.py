"""This repo is public. Personal state (secrets, real config, the data repo) must never
be tracked; this test fails CI if it is."""

import re
import shutil
import subprocess

import pytest

from tests.conftest import ROOT


def tracked_files():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_no_personal_state_is_tracked():
    bad = [f for f in tracked_files()
           if f.startswith(("config/", "data/"))
           or re.search(r"(^|/)\.env$|\.env$", f) and not f.endswith(".env.example")
           or f.endswith((".db", ".db-wal", ".db-shm"))]
    assert bad == [], f"personal files tracked in the public repo: {bad}"


def test_no_secret_looking_values_in_tracked_text():
    anthropic_prefix = "sk-" + "ant-"  # split so this file doesn't match itself
    pattern = re.compile(r"^[ \t]*(KITE_API_KEY|KITE_API_SECRET|ANTHROPIC_API_KEY)[ \t]*=[ \t]*\S+", re.M)
    hits = []
    for name in tracked_files():
        path = ROOT / name
        if path.suffix in {".zip", ".png"} or not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        if pattern.search(text) or anthropic_prefix in text:
            hits.append(name)
    assert hits == [], f"secret-looking values in: {hits}"
