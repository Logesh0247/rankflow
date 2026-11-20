"""Migration 002: add jobs.current_stage (Prompt 02).

Prompt 01 databases do not have this column and SQLAlchemy's
create_all() never adds columns to an existing table, so run this
once against any database created before Prompt 02. Idempotent:
it checks the table first and does nothing if the column exists.

Usage:
    .venv/bin/python scripts/migrate_002_add_current_stage.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from sqlalchemy import text

from app.database.session import make_engine


def main() -> None:
    engine = make_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(jobs)"))]
        else:
            cols = [row[0] for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'jobs'"))]
        if "current_stage" in cols:
            print("jobs.current_stage already exists — nothing to do.")
            return
        conn.execute(text(
            "ALTER TABLE jobs ADD COLUMN current_stage VARCHAR(32) "
            "NOT NULL DEFAULT 'INITIALIZED'"))
        print("Added jobs.current_stage (default 'INITIALIZED').")


if __name__ == "__main__":
    main()
