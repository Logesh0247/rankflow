"""Migration 005: add Ranking score breakdown (Prompt 06).

Adds the `score_breakdown` JSON column to ranking_results: the
five component scores + total (0-100) that produced the row's
`score`. SQLAlchemy's create_all() never adds columns to an
existing table, so run this once against any database created
before Prompt 06. Idempotent: checks the column first.

Usage:
    .venv/bin/python scripts/migrate_005_add_ranking_breakdown.py
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
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(ranking_results)"))}
        else:
            existing = {row[0] for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'ranking_results'"))}
        if "score_breakdown" in existing:
            print("ranking_results.score_breakdown already exists — nothing to do.")
        else:
            conn.execute(text("ALTER TABLE ranking_results ADD COLUMN score_breakdown JSON"))
            print("Added ranking_results.score_breakdown (JSON).")
    print("Note: on PostgreSQL, re-apply app/database/schema.sql for the full DDL.")


if __name__ == "__main__":
    main()
