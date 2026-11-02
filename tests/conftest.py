import shutil
from pathlib import Path

import pytest

from ltinv.config import get_settings
from ltinv.db import repo

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway LTINV_HOME with the repo's sample config and no secrets."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    for key in ("KITE_API_KEY", "KITE_API_SECRET", "LTINV_HOME"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


@pytest.fixture
def settings(home):
    return get_settings(home)


@pytest.fixture
def conn(settings):
    c = repo.connect(settings.db_path)
    repo.migrate(c)
    yield c
    c.close()
