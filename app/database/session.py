"""Momentrix AI — database session and initialisation.

SQLite concurrency (fix for the 2026-11-21 live incident): the
Render database locked ("database is locked") while a long
footage download ran, because the default rollback-journal mode
lets one long-lived transaction block every other writer, and
the default busy timeout is 0 (fail immediately). Every SQLite
engine created here therefore sets, on each new connection:

* ``PRAGMA journal_mode=WAL`` — readers never block writers
  and writers never block readers; the mode is stored in the
  database file, so it survives restarts.
* ``PRAGMA busy_timeout=30000`` — a writer that still has to
  wait (e.g. two writers at once) waits up to 30 s instead of
  failing instantly.

WAL is per-database, busy_timeout is per-connection, so both
are set by a ``connect`` event listener on every engine — that
covers every path that creates an engine: ``init_db.py``,
``migrate_all.py`` (each migration uses ``make_engine``), and
uvicorn startup in ``main.py``. PostgreSQL engines are
unaffected (the pragmas are SQLite-only). Backups use the
sqlite3 online-backup API (``scripts/backup_db.py``), which is
WAL-safe — never copy the live .db file by hand.

Short transactions are the other half of the fix: no service
may hold a transaction open across network I/O or a long
render/download. Video Intelligence commits per candidate and
the footage downloader streams first, then records in a short
session (see ``app/agents/video_intelligence/download.py``).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.models.models import Base

# SQLite write-wait budget (see module docstring): a writer that
# must wait for a lock retries for up to 30 s instead of failing
# immediately with "database is locked".
SQLITE_BUSY_TIMEOUT_MS = 30000


def _sqlite_connect(dbapi_connection, connection_record) -> None:
    """Per-connection SQLite pragmas: WAL mode + busy timeout."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def make_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = settings.database_url
    if url.startswith("sqlite"):
        path = url.replace("sqlite:///", "")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})
        event.listen(engine, "connect", _sqlite_connect)
        return engine
    return create_engine(url, pool_pre_ping=True)


def init_db(engine: Engine | None = None, settings: Settings | None = None) -> Engine:
    """Create all tables. Safe to call repeatedly (create_all is idempotent)."""
    engine = engine or make_engine(settings)
    Base.metadata.create_all(engine)
    return engine


def check_db(engine: Engine | None = None) -> bool:
    """Return True only if a trivial query succeeds."""
    engine = engine or make_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def get_session(engine: Engine | None = None) -> Session:
    engine = engine or make_engine()
    return sessionmaker(bind=engine)()
