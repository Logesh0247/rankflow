"""RANKFLOW AI — core configuration.

All configuration comes from environment variables. No API key or
secret is hardcoded anywhere in this project. Copy `.env.example`
to `.env` and fill in values for the integrations you actually use.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings, loaded from the environment / .env file."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Momentrix"
    app_env: Literal["development", "test", "production"] = "development"
    app_debug: bool = False
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"

    # Database. Default is a local SQLite file (local testing only).
    # Production target is PostgreSQL, e.g.
    #   postgresql+psycopg://user:password@localhost:5432/rankflow
    database_url: str = Field(default=f"sqlite:///{PROJECT_ROOT / 'data' / 'rankflow.db'}")

    # Media / object storage. Default is the local filesystem.
    # S3-compatible storage is an integration point (Prompt 10+); the
    # settings below are the only place S3 credentials are read from.
    storage_backend: Literal["local", "s3"] = "local"
    storage_root: Path = PROJECT_ROOT / "data" / "storage"
    # Minimum free disk space (MB) under the storage root before
    # storage validation warns. Lower it for small tmpfs test roots.
    storage_min_free_mb: int = 500
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = "us-east-1"

    # Background processing. Redis + Celery are installed as libraries
    # but no Redis server is present in this environment (see
    # docs/ARCHITECTURE.md). Leave the broker unset for synchronous
    # local runs; set it when a Redis server is available.
    redis_url: str = ""
    celery_broker_url: str = ""

    # External APIs — all optional in Prompt 01, none verified yet.
    # Muse AI is the reasoning provider named in the brief; no Muse
    # API key or endpoint is configured here.
    youtube_api_key: str = ""
    # Narration TTS (Edge TTS — added after Prompt 12, user-approved).
    # edge-tts is a free, unofficial client of Microsoft's Edge
    # neural-voice endpoint: no API key, no account, no per-character
    # charge. TTS_PROVIDER defaults to "edge-tts" and TTS_ENABLED to
    # true, so renders get a real narration voice by default.
    # tts_api_key is kept for other providers and is NOT needed by
    # edge-tts — never invent or require a key for it.
    # Commercial-use caveat: edge-tts has no SLA and its use of
    # Microsoft's endpoint may be rate-limited; check the
    # commercial-use terms before publishing narrated videos.
    # LIVE SYNTHESIS IS UNVERIFIED ON THIS MACHINE: the network
    # proxy blocks speech.platform.bing.com (SSL WRONG_VERSION_NUMBER),
    # so a failed synthesis here proves nothing about the deploy host.
    # A synthesis failure never hard-fails a job — production falls
    # back to a silent narration track and QA reports needs_review.
    tts_provider: str = "edge-tts"
    tts_voice: str = "en-US-GuyNeural"
    tts_enabled: bool = True
    tts_timeout_seconds: float = 120.0
    tts_api_key: str = ""
    speech_api_key: str = ""

    # Research Agent (Prompt 03). Primary discovery provider:
    # "internet_archive" (real, no key — verified live), "youtube"
    # (implemented, needs YOUTUBE_API_KEY, falls back to Archive),
    # "test" (TEST-ONLY deterministic provider, never the default).
    research_provider: Literal["internet_archive", "youtube", "test"] = "internet_archive"
    research_max_candidates: int = 10
    research_timeout_seconds: float = 15.0
    research_max_attempts: int = 3
    research_enrich_duration: bool = True

    # Rights Verification Agent (Prompt 04). When a candidate's
    # provider published no licence, the agent re-checks the
    # Internet Archive metadata API for a licenseurl before giving
    # up — provider-published metadata, fetched live, never invented.
    # A failed lookup leaves the candidate NEEDS_REVIEW.
    rights_fetch_ia_metadata: bool = True
    rights_ia_metadata_timeout_seconds: float = 10.0

    # Video Intelligence Agent (Prompt 05). Analysis is a
    # deterministic heuristic over verifiable frame-level signals
    # (scene changes, motion, brightness, sharpness) — analyser
    # "heuristic_frame_signals_v1". No vision-language model or
    # API key is configured in this environment, so no semantic
    # event is ever inferred from footage; descriptions say so.
    vi_sample_fps: float = 1.0
    vi_max_sampled_frames: int = 600
    vi_scene_threshold: float = 0.55
    vi_motion_threshold: float = 6.0
    vi_min_moment_seconds: float = 2.0
    vi_max_moment_seconds: float = 12.0
    vi_max_moments_per_video: int = 5
    vi_lead_in_seconds: float = 0.5
    vi_aftermath_seconds: float = 0.5
    vi_analyser: str = "heuristic_frame_signals_v1"

    # Ranking Agent (Prompt 06). Scores are editorial heuristics
    # from the measured frame signals (scorer
    # "heuristic_editorial_v1") — NOT viral-performance
    # probabilities. ranking_min_score is the 0-100 floor below
    # which a moment is ineligible; duplicate IoU and the
    # cross-upload fingerprint tolerance are the Phase 3 rules.
    ranking_min_score: float = 20.0
    ranking_duplicate_iou: float = 0.5
    ranking_fingerprint_tolerance: float = 0.10
    ranking_scorer: str = "heuristic_editorial_v1"

    # Creative Director Agent (Prompt 07). Deterministic template
    # generator ("deterministic_template_v1") — no LLM is configured,
    # so narration is built from measured frame signals only.
    # cd_target_duration_seconds is the intended Short length; the
    # actual timeline is intro + clips + transitions + outro.
    cd_target_duration_seconds: float = 60.0
    cd_intro_seconds: float = 3.0
    cd_outro_seconds: float = 3.0
    cd_transition_seconds: float = 0.5
    cd_narration_wpm: float = 150.0
    cd_generator: str = "deterministic_template_v1"

    # Video Production Agent (Prompt 08; narration via Edge TTS added
    # after Prompt 12). Renders the approved EDL with FFmpeg to
    # 1080x1920 @ 30fps H.264/AAC. Narration audio is generated with
    # edge-tts by default (TTS_PROVIDER=edge-tts, TTS_VOICE, no API
    # key); a synthesis failure falls back to a silent narration
    # track and is recorded in the manifest, never faked. Music/SFX
    # are only used when a directory of licensed audio files is
    # configured below and the files probe as audio; otherwise the
    # render has no music or SFX and the manifest says so. Nothing
    # here invents an asset: a missing asset is recorded, never faked.
    prod_font_file: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    prod_caption_font_file: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    prod_caption_font_size: int = 48
    prod_music_dir: str = ""
    prod_sfx_dir: str = ""
    prod_ffmpeg_preset: str = "veryfast"
    prod_crf: int = 23
    prod_safe_margin_fraction: float = 0.10
    prod_render_timeout_seconds: float = 600.0

    # Quality Assurance Agent (Prompt 09). Inspects the actual
    # rendered MP4 with ffprobe / FFmpeg / OpenCV — no
    # vision-language model or OCR engine is configured, so
    # semantic checks (what happens in a clip, caption text
    # position, unsuitable material) are recorded as NOT TESTED,
    # never passed on a guess. A render whose narration audio was
    # never generated or failed to synthesise, or whose audio is
    # effectively silent, is flagged as an issue and the verdict is
    # needs_review, never pass. A render with generated narration
    # audio that is present and non-silent can reach pass.
    qa_sample_fps: float = 1.0
    qa_max_sampled_frames: int = 120
    qa_black_threshold: float = 8.0
    qa_frozen_diff_threshold: float = 0.5
    qa_silence_db: float = -70.0
    qa_silence_noise_db: float = -50.0
    qa_duration_tolerance_seconds: float = 0.6
    qa_max_repair_attempts: int = 2

    # Limits
    max_topic_length: int = 200
    max_upload_mb: int = 25

    # Failure notifications (Prompt 12, Phase 4). Empty = log only.
    # Set to a Slack/Discord incoming-webhook URL to get a POST on
    # every job failure. The URL is a secret: it lives in the
    # environment only and is never returned by any endpoint.
    notify_webhook_url: str = ""

    # API rate limiting (Prompt 12, Phase 5). Requests per minute per
    # client IP, enforced by in-memory token buckets. 0 disables it.
    # The default is generous for a single-user local service; lower
    # it (e.g. 120) when the API is exposed behind a reverse proxy.
    rate_limit_per_minute: int = 600

    # Orchestrator (Prompt 02). Retries are per stage: a stage is
    # attempted at most `orchestrator_max_attempts` times (first try
    # + retries). Backoff between attempts is
    # base * 2 ** (attempt - 1) seconds. Timeouts are per attempt.
    orchestrator_max_attempts: int = 3
    orchestrator_timeout_seconds: float = 60.0
    orchestrator_backoff_base_seconds: float = 1.0
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, v: str) -> str:
        if not v:
            raise ValueError("DATABASE_URL must not be empty")
        return v

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_settings(env_file: str | None = None) -> Settings:
    """Load settings, optionally from an explicit .env file (tests)."""
    if env_file:
        return Settings(_env_file=env_file)
    return Settings()
