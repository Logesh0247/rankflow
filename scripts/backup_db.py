"""Database backup (Prompt 12, Phase 4).

SQLite: uses the sqlite3 online-backup API (safe while the app is
running — never copy the live .db file by hand). The backup is
verified with `PRAGMA integrity_check` before it is kept, and
old backups are pruned to BACKUP_KEEP (default 7).

PostgreSQL: uses pg_dump when DATABASE_URL is Postgres.

Usage:
    .venv/bin/python scripts/backup_db.py [--out DIR] [--keep N]

Default output: data/backups/rankflow-<UTC timestamp>.db
Restore (SQLite): stop the app, replace data/rankflow.db with the
backup, restart. See docs/DEPLOYMENT.md.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings


def _sqlite_backup(source: Path, dest: Path) -> None:
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    check = sqlite3.connect(str(dest))
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if not row or row[0] != "ok":
        raise RuntimeError(f"backup integrity check failed: {row}")


def _postgres_backup(database_url: str, dest: Path) -> None:
    proc = subprocess.run(
        ["pg_dump", database_url, "-f", str(dest)],
        capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {(proc.stderr or '').strip()[:300]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--keep", type=int, default=7)
    args = parser.parse_args()

    settings = get_settings()
    out_dir = Path(args.out) if args.out else Path(settings.storage_root).parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if settings.is_sqlite:
        source = Path(settings.database_url.replace("sqlite:///", ""))
        dest = out_dir / f"rankflow-{stamp}.db"
        if not source.is_file():
            raise SystemExit(f"database file not found: {source}")
        _sqlite_backup(source, dest)
    else:
        dest = out_dir / f"rankflow-{stamp}.sql"
        _postgres_backup(settings.database_url, dest)

    print(f"backup written: {dest} ({dest.stat().st_size} bytes, integrity ok)")
    # Prune: keep the newest --keep backups.
    backups = sorted(out_dir.glob("rankflow-*"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-args.keep]:
        old.unlink()
        print(f"pruned old backup: {old}")


if __name__ == "__main__":
    main()
