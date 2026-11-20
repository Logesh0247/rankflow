"""Migration 011 — cost events + job_id indexes (Prompt 12).

* Creates the `cost_events` table (Prompt 12, Phase 3).
* Adds job_id indexes on storyboards, narrations and
  edit_instructions (Prompt 12, Phase 2 — those lookups were
  unindexed full scans).

Idempotent: safe to run twice. Works on SQLite and PostgreSQL.
Run: .venv/bin/python scripts/migrate_011_add_cost_events.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text

from app.core.config import get_settings
from app.database.session import make_engine
from app.models.models import Base


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_index(inspector, table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in inspector.get_indexes(table))


def main() -> None:
    settings = get_settings()
    engine = make_engine(settings)
    inspector = inspect(engine)
    with engine.begin() as conn:
        if not _has_table(inspector, "cost_events"):
            Base.metadata.tables["cost_events"].create(conn)
            print("created table cost_events")
        else:
            print("cost_events already exists — nothing to do")
        for table, index in [
            ("storyboards", "ix_storyboards_job"),
            ("narrations", "ix_narrations_job"),
            ("edit_instructions", "ix_edit_instructions_job"),
        ]:
            if _has_table(inspector, table) and not _has_index(inspector, table, index):
                conn.execute(text(f'CREATE INDEX "{index}" ON "{table}" ("job_id")'))
                print(f"created index {index} on {table}")
            else:
                print(f"index {index} already exists — nothing to do")
    print("migration 011 complete")


if __name__ == "__main__":
    main()
