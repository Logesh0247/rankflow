"""Migration 010: allow 'repair_round' in agent_execution_logs.status (Prompt 10).

Prompt 10's repair loop records one marker row per repair round in
`agent_execution_logs` (agent_name 'orchestrator', status
'repair_round') so the round cap survives re-renders. The old CHECK
constraint only allowed started/succeeded/failed/skipped, so the
insert failed. SQLite cannot alter a CHECK constraint in place, so
the table is rebuilt (same columns, new constraint) with all rows
copied over; Postgres drops and re-adds the constraint instead.
Idempotent: does nothing if 'repair_round' is already allowed.

Usage:
    .venv/bin/python scripts/migrate_010_add_repair_round_log_status.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from sqlalchemy import text

from app.database.session import make_engine

NEW_STATUSES = "('started','succeeded','failed','skipped','repair_round')"


def main() -> None:
    engine = make_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            sql = conn.execute(text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'agent_execution_logs'"
            )).scalar_one()
            if "'repair_round'" in sql:
                print("agent_execution_logs already allows 'repair_round' — nothing to do.")
                return
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            conn.execute(text(
                "CREATE TABLE agent_execution_logs_new ("
                "id VARCHAR(36) NOT NULL, job_id VARCHAR(36) NOT NULL, "
                "agent_name VARCHAR(64) NOT NULL, task_id VARCHAR(36), "
                "status VARCHAR(32) NOT NULL, detail TEXT, duration_ms INTEGER, "
                "created_at DATETIME NOT NULL, PRIMARY KEY (id), "
                f"CONSTRAINT ck_agent_log_status CHECK (status IN {NEW_STATUSES}), "
                "FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE)"
            ))
            conn.execute(text(
                "INSERT INTO agent_execution_logs_new "
                "(id, job_id, agent_name, task_id, status, detail, duration_ms, created_at) "
                "SELECT id, job_id, agent_name, task_id, status, detail, duration_ms, created_at "
                "FROM agent_execution_logs"
            ))
            conn.execute(text("DROP TABLE agent_execution_logs"))
            conn.execute(text("ALTER TABLE agent_execution_logs_new RENAME TO agent_execution_logs"))
            conn.execute(text(
                "CREATE INDEX ix_agent_logs_job_agent "
                "ON agent_execution_logs (job_id, agent_name)"))
            conn.execute(text("PRAGMA foreign_keys = ON"))
            print("Rebuilt agent_execution_logs: 'repair_round' now allowed (rows preserved).")
        else:
            conn.execute(text(
                "ALTER TABLE agent_execution_logs DROP CONSTRAINT ck_agent_log_status"))
            conn.execute(text(
                "ALTER TABLE agent_execution_logs ADD CONSTRAINT ck_agent_log_status "
                f"CHECK (status IN {NEW_STATUSES})"))
            print("Updated ck_agent_log_status on Postgres: 'repair_round' now allowed.")


if __name__ == "__main__":
    main()
