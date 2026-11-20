"""Settings status endpoint (Prompt 11 — Settings page).

GET /settings/status reports configuration *status* only: which
providers are configured, which audio directories exist, video
output settings, storage status, and per-agent health. It never
returns a secret, API key, or credential value — only booleans and
non-sensitive settings.
"""
from __future__ import annotations

import shutil

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.database.session import make_engine
from app.orchestrator.registry import build_default_registry
from app.utils.paths import which_ffmpeg

router = APIRouter(tags=["settings"])


class SettingsStatus(BaseModel):
    app_name: str
    app_env: str
    database: str
    storage_backend: str
    storage_root_exists: bool
    storage_free_gb: float | None = None
    ffmpeg_ok: bool
    ffprobe_ok: bool
    research_provider: str
    youtube_configured: bool
    tts_provider: str
    tts_configured: bool
    music_library_configured: bool
    sfx_library_configured: bool
    video_output: dict = Field(default_factory=dict)
    narration: dict = Field(default_factory=dict)
    qa: dict = Field(default_factory=dict)
    agents: list[dict] = Field(default_factory=list)


@router.get("/settings/status", response_model=SettingsStatus)
def settings_status() -> SettingsStatus:
    settings = get_settings()
    tools = which_ffmpeg(settings.ffmpeg_path, settings.ffprobe_path)
    free_gb = None
    try:
        free_gb = round(shutil.disk_usage(settings.storage_root).free / 1e9, 2)
    except OSError:
        free_gb = None
    agents: list[dict] = []
    try:
        for descriptor in build_default_registry(make_engine()).list():
            agents.append({"id": descriptor.agent_id, "name": descriptor.name,
                           "status": descriptor.status})
    except Exception:
        agents = []
    return SettingsStatus(
        app_name=settings.app_name,
        app_env=settings.app_env,
        database="postgresql" if settings.is_postgres else "sqlite (local)",
        storage_backend=settings.storage_backend,
        storage_root_exists=settings.storage_root.exists(),
        storage_free_gb=free_gb,
        ffmpeg_ok=bool(tools.get("ffmpeg")),
        ffprobe_ok=bool(tools.get("ffprobe")),
        research_provider=settings.research_provider,
        youtube_configured=bool(settings.youtube_api_key),
        tts_provider=settings.tts_provider or "not configured",
        tts_configured=bool(settings.tts_provider and settings.tts_api_key),
        music_library_configured=bool(settings.prod_music_dir),
        sfx_library_configured=bool(settings.prod_sfx_dir),
        video_output={"width": 1080, "height": 1920, "fps": 30,
                      "video_codec": "H.264", "audio_codec": "AAC",
                      "crf": settings.prod_crf, "preset": settings.prod_ffmpeg_preset,
                      "target_duration_seconds": settings.cd_target_duration_seconds},
        narration={"generator": settings.cd_generator,
                   "words_per_minute": settings.cd_narration_wpm,
                   "intro_seconds": settings.cd_intro_seconds,
                   "outro_seconds": settings.cd_outro_seconds,
                   "transition_seconds": settings.cd_transition_seconds},
        qa={"max_repair_attempts": settings.qa_max_repair_attempts,
            "duration_tolerance_seconds": settings.qa_duration_tolerance_seconds},
        agents=agents,
    )
