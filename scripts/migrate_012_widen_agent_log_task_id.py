"""Migration 012: widen agent_execution_logs.task_id to VARCHAR(64).

Root cause (2026-11-21 Neon incident): the orchestrator builds
task IDs as "{job_id}:{stage}:{attempt}" (app/orchestrator/engine.py)
— a 36-char UUID plus a stage name plus an attempt digit, e.g.
"ee5b5b34-0639-44cb-9728-00d6247cbcfb:RESEARCH:1" is 47 chars and
the longest possible value ("…:RIGHTS_VERIFICATION:3") is 58.
The column was VARCHAR(36). SQLite never enforces a VARCHAR
length, so every job worked on SQLite; PostgreSQL does enforce
it, so on Neon every job failed at RESEARCH with
(psycopg.errors.StringDataRightTruncation) value too long for
type character varying(36).

How this reaches the existing live Neon database: the Render
Dockerfile CMD runs `python scripts/init_db.py &&
python scripts/migrate_all.py && exec uvicorn …` on every deploy,
so this migration runs at container start, before the API serves
traffic. `init_db` (create_all) only creates missing tables and
never alters an existing column, so the ALTER below is what
actually fixes the live database. It is a widening-only change
(VARCHAR(36) -> VARCHAR(64)): metadata-only on PostgreSQL, no
rows are rewritten, existing rows are unaffected (no stored
task_id can exceed 36 chars today — the long inserts all failed),
and it is safe to run while no job is running (it runs before
uvicorn starts).

Idempotent: on PostgreSQL it reads the current length from
information_schema and does nothing when it is already >= 64;
on SQLite VARCHAR length is unenforced, so nothing is needed
there (the model change alone covers fresh SQLite databases).
Safe to run twice — migrate_all.py runs it on every deploy.

Usage:
    .venv/bin/python scripts/migrate_012_widen_agent_log_task_id.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.database.session import make_engine

TABLE = "agent_execution_logs"
COLUMN = "task_id"
TARGET_LENGTH = 64


def widen_task_id(engine: Engine) -> str:
    """Widen task_id to VARCHAR(64) if needed. Returns what happened."""
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            # SQLite does not enforce VARCHAR lengths, so the old
            # VARCHAR(36) DDL never truncated anything here; the
            # model change (String(64)) covers fresh SQLite DBs.
            # No table rebuild: it would copy every row for no gain.
            exists = conn.execute(text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = :table"
            ), {"table": TABLE}).first()
            if not exists:
                return (f"{TABLE} does not exist yet — nothing to do "
                        "(init_db will create it with VARCHAR(64)).")
            return (f"SQLite: {TABLE}.{COLUMN} length is unenforced — "
                    "nothing to do.")
        # PostgreSQL: widen in place, only if still narrower than 64.
        length = conn.execute(text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ), {"table": TABLE, "column": COLUMN}).scalar_one_or_none()
        if length is None:
            return (f"{TABLE}.{COLUMN} does not exist — nothing to do "
                    "(init_db will create it with VARCHAR(64)).")
        if length >= TARGET_LENGTH:
            return (f"{TABLE}.{COLUMN} is already VARCHAR({length}) — "
                    "nothing to do.")
        conn.execute(text(
            f"ALTER TABLE {TABLE} "
            f"ALTER COLUMN {COLUMN} TYPE VARCHAR({TARGET_LENGTH})"
        ))
        return (f"Widened {TABLE}.{COLUMN} "
                f"VARCHAR({length}) -> VARCHAR({TARGET_LENGTH}).")


def main() -> None:
    print(widen_task_id(make_engine()))
    print("migration 012 complete")


if __name__ == "__main__":
    main()
