"""Momentrix AI — database session and initialisation."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.models.models import Base


def make_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = settings.database_url
    if url.startswith("sqlite"):
        path = url.replace("sqlite:///", "")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url, connect_args={"check_same_thread": False})
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
