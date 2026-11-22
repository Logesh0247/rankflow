"""Health endpoint — the only read that needs no database write."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.notifications import recent_notifications
from app.database.session import check_db, get_engine
from app.schemas.schemas import HealthResponse
from app.storage.validate import validate_storage
from app.utils.paths import which_ffmpeg

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    tools = which_ffmpeg(settings.ffmpeg_path, settings.ffprobe_path)
    db_ok = check_db()
    ffmpeg_ok = bool(tools.get("ffmpeg"))
    return HealthResponse(
        status="ok" if (db_ok and ffmpeg_ok) else "degraded",
        app="Momentrix",
        database_ok=db_ok,
        ffmpeg_ok=ffmpeg_ok,
        storage_backend=settings.storage_backend,
    )


@router.get("/health/detailed")
def health_detailed() -> dict:
    """Prompt 12, Phase 4: deeper health — storage validation plus the
    most recent failure notifications. No secret is ever included."""
    settings = get_settings()
    base = health()
    try:
        storage = validate_storage(get_engine(), settings)
    except Exception as exc:  # noqa: BLE001 — report, don't fail the read
        storage = {"ok": False, "issues": [f"storage validation failed: {exc}"], "checks": {}}
    return {
        "status": base.status,
        "app": base.app,
        "database_ok": base.database_ok,
        "ffmpeg_ok": base.ffmpeg_ok,
        "storage_backend": base.storage_backend,
        "storage": storage,
        "recent_failures": recent_notifications(settings, limit=5),
    }
