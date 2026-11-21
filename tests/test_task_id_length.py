"""Regression test for the 2026-11-21 Neon incident: every job failed
at RESEARCH with (psycopg.errors.StringDataRightTruncation) value
too long for type character varying(36).

Root cause: the orchestrator builds task IDs as
"{job_id}:{stage}:{attempt}" — a 36-char UUID plus a stage name
plus an attempt digit — but agent_execution_logs.task_id was
VARCHAR(36). SQLite never enforces VARCHAR lengths, so the bug
was invisible until the live database moved to Neon Postgres.

The fix (migration 012): task_id is VARCHAR(64). The longest
possible value is 36 + 1 + 19 (RIGHTS_VERIFICATION) + 1 + 1 = 58
chars, so 64 fits with headroom.

There is no live PostgreSQL server in the test environment, so
the Postgres enforcement is reproduced as the length check
Postgres itself applies: len(value) must be <= the column's
declared length — plus a real round-trip insert through
JobService.record_log, which is the exact call that failed live.
"""
import uuid

import pytest
from sqlalchemy import text

from app.models.models import AgentExecutionLog
from app.orchestrator.jobs import JobService
from app.orchestrator.states import AGENT_STAGES
from app.schemas.schemas import JobCreate
from scripts.migrate_012_widen_agent_log_task_id import widen_task_id

# The exact job and task_id from the live failure.
FAILED_JOB_ID = "ee5b5b34-0639-44cb-9728-00d6247cbcfb"
FAILED_TASK_ID = f"{FAILED_JOB_ID}:RESEARCH:1"  # 47 chars > 36


def _column_length() -> int:
    return AgentExecutionLog.__table__.c.task_id.type.length


def _postgres_would_accept(task_id: str) -> bool:
    """The length check PostgreSQL applies to a VARCHAR(n) column."""
    return len(task_id) <= _column_length()


def test_model_column_is_wide_enough_for_every_task_id():
    assert _column_length() == 64
    assert len(FAILED_TASK_ID) == 47
    assert _postgres_would_accept(FAILED_TASK_ID)


def test_longest_possible_task_id_fits():
    job_id = str(uuid.uuid4())
    longest_stage = max((s.value for s in AGENT_STAGES), key=len)
    assert longest_stage == "RIGHTS_VERIFICATION"
    task_id = f"{job_id}:{longest_stage}:3"
    assert len(task_id) == 58
    assert _postgres_would_accept(task_id)


def test_every_stage_task_id_fits():
    job_id = str(uuid.uuid4())
    for stage in AGENT_STAGES:
        for attempt in (1, 2, 3):
            task_id = f"{job_id}:{stage.value}:{attempt}"
            assert _postgres_would_accept(task_id), task_id


def test_exact_failed_task_id_stores_and_reads_back(engine):
    """The insert that failed live must now succeed and be findable
    by the idempotency check (has_succeeded_task)."""
    job = JobService(engine).create_job(
        JobCreate(topic="TOP 5 CRAZIEST PARKOUR FAILS"))
    service = JobService(engine)
    service.record_log(job.id, "research", FAILED_TASK_ID, "succeeded",
                       detail='{"output": {}}')
    assert service.has_succeeded_task(FAILED_TASK_ID) is True
    # And the longest possible value stores too.
    longest = f"{job.id}:RIGHTS_VERIFICATION:3"
    service.record_log(job.id, "rights", longest, "succeeded",
                       detail='{"output": {}}')
    assert service.has_succeeded_task(longest) is True


def test_migration_012_is_idempotent_on_sqlite(engine):
    """Running the migration twice against the test database must
    succeed both times (migrate_all.py runs it on every deploy)."""
    first = widen_task_id(engine)
    second = widen_task_id(engine)
    assert "nothing to do" in first
    assert "nothing to do" in second


def test_migration_012_handles_a_legacy_varchar36_database(tmp_path):
    """A database created with the old VARCHAR(36) DDL (like the
    live Neon database before the fix) must not break the migration
    — on SQLite the length is unenforced, so it is a no-op, and the
    long task_id still stores."""
    from app.core.config import Settings
    from app.database.session import make_engine

    db_path = tmp_path / "legacy.db"
    legacy = make_engine(Settings(database_url=f"sqlite:///{db_path}"))
    with legacy.begin() as conn:
        conn.execute(text(
            "CREATE TABLE agent_execution_logs ("
            "id VARCHAR(36) NOT NULL, job_id VARCHAR(36) NOT NULL, "
            "agent_name VARCHAR(64) NOT NULL, task_id VARCHAR(36), "
            "status VARCHAR(32) NOT NULL, detail TEXT, "
            "duration_ms INTEGER, created_at DATETIME NOT NULL, "
            "PRIMARY KEY (id))"))
    assert "nothing to do" in widen_task_id(legacy)
    assert "nothing to do" in widen_task_id(legacy)
