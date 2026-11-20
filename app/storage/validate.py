"""Storage validation (Prompt 12, Phase 4).

Checks the things a render depends on before they bite at 2 a.m.:
the storage root exists and is writable, the database file exists,
every rendered video row points at a file that is actually on disk,
and there is free disk space for the next render. Returns a report
with `ok` and a list of issues — it never raises, so startup can
log the report as warnings instead of refusing to boot.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.models.models import RenderedVideo

# A single Short is ~3-15 MB; refuse to call storage healthy with
# less than `storage_min_free_mb` free — a render + segments + QA
# sampling needs room. The floor is a setting (default 500 MB) so
# small tmpfs test roots can lower it.
def _min_free_bytes(settings: Settings) -> int:
    return max(0, int(settings.storage_min_free_mb)) * 1024 * 1024


def validate_storage(engine: Engine, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    issues: list[str] = []
    checks: dict = {}

    root = Path(settings.storage_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks["storage_root_writable"] = True
    except OSError as exc:
        checks["storage_root_writable"] = False
        issues.append(f"storage root {root} is not writable: {exc}")

    db_path = Path(settings.database_url.replace("sqlite:///", "")) \
        if settings.is_sqlite else None
    if db_path is not None:
        checks["database_file_exists"] = db_path.is_file()
        if not db_path.is_file():
            issues.append(f"database file does not exist: {db_path}")
    else:
        checks["database_file_exists"] = None  # PostgreSQL: checked by check_db()

    free = shutil.disk_usage(root).free if root.exists() else 0
    min_free = _min_free_bytes(settings)
    checks["disk_free_bytes"] = free
    checks["disk_free_ok"] = free >= min_free
    if free < min_free:
        issues.append(
            f"only {free / (1024 ** 2):.0f} MB free under {root} "
            f"(minimum {min_free / (1024 ** 2):.0f} MB)")

    sessions = sessionmaker(bind=engine)
    missing = 0
    total = 0
    with sessions() as session:
        rows = session.query(RenderedVideo).all()
        for row in rows:
            if not row.file_path:
                continue
            total += 1
            if not Path(row.file_path).is_file():
                missing += 1
    checks["rendered_videos_checked"] = total
    checks["rendered_videos_missing"] = missing
    if missing:
        issues.append(f"{missing} of {total} rendered video file(s) missing from disk")

    return {"ok": not issues, "issues": issues, "checks": checks}
