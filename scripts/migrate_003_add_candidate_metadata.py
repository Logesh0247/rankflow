"""Migration 003: add candidate provenance columns (Prompt 03).

Adds creator, description and licence to candidate_videos. These
are provider-published provenance fields (None = not published, never
invented). SQLAlchemy's create_all() never adds columns to an
existing table, so run this once against any database created before
Prompt 03. Idempotent: checks each column first.

Usage:
    .venv/bin/python scripts/migrate_003_add_candidate_metadata.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from sqlalchemy import text

from app.database.session import make_engine

COLUMNS = {
    "creator": "VARCHAR(500)",
    "description": "TEXT",
    "licence": "VARCHAR(255)",
}


def main() -> None:
    engine = make_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(candidate_videos)"))}
        else:
            existing = {row[0] for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'candidate_videos'"))}
        for column, ddl in COLUMNS.items():
            if column in existing:
                print(f"candidate_videos.{column} already exists — nothing to do.")
                continue
            conn.execute(text(f"ALTER TABLE candidate_videos ADD COLUMN {column} {ddl}"))
            print(f"Added candidate_videos.{column} ({ddl}).")


if __name__ == "__main__":
    main()
