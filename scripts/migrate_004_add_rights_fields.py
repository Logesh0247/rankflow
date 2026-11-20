"""Migration 004: add Rights Verification fields (Prompt 04).

Adds the Phase 2 evidence columns to rights_evidence: licence
name/URL, permission evidence, verification date/method, the three
permission flags, attribution, restrictions, expiry and reviewer
fields. SQLAlchemy's create_all() never adds columns to an
existing table, so run this once against any database created
before Prompt 04. Idempotent: checks each column first.

Also notes the verdict CHECK: pre-Prompt-04 databases have
ck_rights_verdict without 'expired'. SQLite does not enforce CHECK
constraints added by create_all on existing tables anyway, and
PostgreSQL DDL is regenerated in app/database/schema.sql — apply
that file (or ALTER the constraint) on a live Postgres database.

Usage:
    .venv/bin/python scripts/migrate_004_add_rights_fields.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from sqlalchemy import text

from app.database.session import make_engine

COLUMNS = {
    "license_name": "VARCHAR(255)",
    "license_url": "TEXT",
    "permission_evidence": "TEXT",
    "verification_date": "TIMESTAMP WITH TIME ZONE",
    "commercial_use": "BOOLEAN",
    "modification_allowed": "BOOLEAN",
    "redistribution_allowed": "BOOLEAN",
    "attribution_required": "BOOLEAN",
    "attribution_text": "TEXT",
    "geographic_restrictions": "TEXT",
    "time_restrictions": "TEXT",
    "expires_at": "TIMESTAMP WITH TIME ZONE",
    "verification_method": "VARCHAR(64)",
    "reviewer_status": "VARCHAR(32)",
    "reviewed_by": "VARCHAR(120)",
    "reviewed_at": "TIMESTAMP WITH TIME ZONE",
    "reasons": "JSON",
}


def main() -> None:
    engine = make_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(rights_evidence)"))}
        else:
            existing = {row[0] for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'rights_evidence'"))}
        for column, ddl in COLUMNS.items():
            if column in existing:
                print(f"rights_evidence.{column} already exists — nothing to do.")
                continue
            conn.execute(text(f"ALTER TABLE rights_evidence ADD COLUMN {column} {ddl}"))
            print(f"Added rights_evidence.{column} ({ddl}).")
    print("Note: on PostgreSQL, re-apply app/database/schema.sql (or ALTER the "
          "ck_rights_verdict constraint) so the CHECK includes 'expired'.")


if __name__ == "__main__":
    main()
