"""Pytest fixtures: isolated temp SQLite DB per test session."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="rankflow-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{TMP / 'test.db'}"
os.environ["STORAGE_ROOT"] = str(TMP / "storage")


@pytest.fixture(scope="session")
def engine():
    from app.database.session import init_db, make_engine
    from app.core.config import Settings

    settings = Settings(database_url=f"sqlite:///{TMP / 'test.db'}", storage_root=TMP / "storage")
    eng = make_engine(settings)
    init_db(eng)
    return eng
