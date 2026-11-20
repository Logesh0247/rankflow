"""Migration 009: add the qa_reports table (Prompt 09).

Creates the `qa_reports` table that the Quality Assurance Agent
persists its reports into: one row per QA run, with the verdict,
the full report JSON (technical / content / visual / audio /
rights validations, checks, recommendations) in `checks`, and
issues / warnings / repair requests in `issues`. SQLAlchemy's
create_all() never adds a table to a database that already
exists without it, so run this once against any database created
before Prompt 09. Idempotent: checks for the table first.

Usage:
    .venv/bin/python scripts/migrate_009_add_qa_report.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from sqlalchemy import text

from app.database.session import make_engine

CREATE_QA_REPORTS = """
CREATE TABLE qa_reports (
    id VARCHAR(36) NOT NULL,
    job_id VARCHAR(36) NOT NULL,
    rendered_video_id VARCHAR(36),
    verdict VARCHAR(32) NOT NULL,
    checks JSON,
    issues JSON,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_qa_verdict CHECK (verdict IN ('pass','fail','needs_review','not_tested')),
    FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE,
    FOREIGN KEY(rendered_video_id) REFERENCES rendered_videos (id)
)
"""


def main() -> None:
    engine = make_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            exists = conn.execute(text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'qa_reports'")).first() is not None
        else:
            exists = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'qa_reports'")).first() is not None
        if exists:
            print("qa_reports already exists — nothing to do.")
        else:
            conn.execute(text(CREATE_QA_REPORTS))
            print("Created qa_reports table.")
    print("Note: on PostgreSQL, re-apply app/database/schema.sql for the full DDL.")


if __name__ == "__main__":
    main()
