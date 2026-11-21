"""Momentrix AI — shared data contracts (Phase 5).

Every agent exchanges these Pydantic models, never ad-hoc dicts.
All models validate on construction; `model_validate` is the
enforcement point at agent boundaries. Enums are plain strings so
JSON stays stable across Python / Postgres / SQLite.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

JobStatus = Literal["created", "running", "awaiting_approval", "approved", "failed", "cancelled"]
CandidateStatus = Literal["discovered", "rights_pending", "rights_approved", "rights_rejected", "analyzed", "selected", "rejected"]
RightsVerdict = Literal["approved", "rejected", "needs_review", "expired", "not_checked"]
QAVerdict = Literal["pass", "fail", "needs_review", "not_tested"]
AgentLogStatus = Literal["started", "succeeded", "failed", "skipped"]


# ---------------------------------------------------------------- jobs
class JobCreate(BaseModel):
    topic: str = Field(..., min_length=1, max_length=200)
    requested_by: str = Field(default="user", max_length=120)

    @field_validator("topic")
    @classmethod
    def _strip_topic(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("topic must not be empty")
        return v


class JobRead(BaseModel):
    id: str
    topic: str
    status: JobStatus
    current_stage: str = "INITIALIZED"
    requested_by: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------- agent tasks
class AgentTask(BaseModel):
    """A unit of work handed to one agent by the orchestrator (Prompt 02)."""

    task_id: str
    job_id: str
    agent_name: str = Field(..., pattern=r"^[a-z_]+$")
    input: dict = Field(default_factory=dict)


class AgentResponse(BaseModel):
    task_id: str
    job_id: str
    agent_name: str
    status: AgentLogStatus
    output: dict = Field(default_factory=dict)
    error: str | None = None
    duration_ms: int | None = None


# ---------------------------------------------------------------- sources / candidates
class SourceCreate(BaseModel):
    platform: str = Field(..., max_length=64)
    external_id: str = Field(..., max_length=255)
    title: str | None = None
    url: str | None = None


class SourceRead(SourceCreate):
    id: str
    created_at: datetime


class CandidateVideoCreate(BaseModel):
    job_id: str
    source_id: str
    external_video_id: str = Field(..., max_length=255)
    title: str | None = None
    url: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    view_count: int | None = Field(default=None, ge=0)
    relevance_score: float | None = Field(default=None, ge=0, le=1)
    # Prompt 03: provider-published provenance (None = not published).
    creator: str | None = None
    description: str | None = None
    licence: str | None = None


class CandidateVideoRead(CandidateVideoCreate):
    id: str
    status: CandidateStatus = "discovered"
    published_at: datetime | None = None
    created_at: datetime


# ---------------------------------------------------------------- rights
class RightsVerificationCreate(BaseModel):
    candidate_video_id: str
    licence: str | None = None
    evidence_url: str | None = None
    notes: str | None = None
    # Prompt 04, Phase 2 — full evidence record (all optional except
    # the candidate id; None means "not supplied", never invented).
    license_name: str | None = None
    license_url: str | None = None
    permission_evidence: str | None = None
    verification_method: str | None = None
    geographic_restrictions: str | None = None
    time_restrictions: str | None = None
    expires_at: datetime | None = None


class RightsVerificationRead(RightsVerificationCreate):
    id: str
    verdict: RightsVerdict = "not_checked"
    checked_by: str = "rights_agent"
    created_at: datetime
    verification_date: datetime | None = None
    commercial_use: bool = False
    modification_allowed: bool = False
    redistribution_allowed: bool = False
    attribution_required: bool = False
    attribution_text: str | None = None
    reviewer_status: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    reasons: list[str] = Field(default_factory=list)


class RightsReviewRequest(BaseModel):
    """Human review of one candidate (POST /jobs/{id}/rights/{candidate_id}/review).

    The reviewer supplies evidence; the agent re-evaluates and
    decides. `permission_granted` is the reviewer's assertion that
    the rightsholder granted permission — it REQUIRES
    `permission_evidence` saying where the grant is recorded, and
    it cannot override a licence that forbids the intended use.
    """

    reviewer: str = Field(..., min_length=1, max_length=120)
    licence_name: str | None = None
    licence_url: str | None = None
    permission_evidence: str | None = None
    permission_granted: bool = False
    expires_at: datetime | None = None
    geographic_restrictions: str | None = None
    time_restrictions: str | None = None
    notes: str | None = None


class AttributionRead(BaseModel):
    """Attribution for one APPROVED candidate, for the Creative
    Director / Production agents (Phase 5)."""

    candidate_video_id: str
    title: str | None = None
    attribution_text: str | None = None
    attribution_required: bool = False
    licence_name: str | None = None


# ---------------------------------------------------------------- moments / ranking
class VideoMomentCreate(BaseModel):
    job_id: str
    candidate_video_id: str
    start_seconds: float = Field(..., ge=0)
    end_seconds: float = Field(..., gt=0)
    description: str | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    labels: dict = Field(default_factory=dict)

    @field_validator("end_seconds")
    @classmethod
    def _end_after_start(cls, v: float, info) -> float:
        start = info.data.get("start_seconds")
        if start is not None and v <= start:
            raise ValueError("end_seconds must be greater than start_seconds")
        return v


class VideoMomentRead(VideoMomentCreate):
    id: str
    created_at: datetime


class RankingResultCreate(BaseModel):
    job_id: str
    moment_id: str
    candidate_video_id: str
    rank: int = Field(..., ge=1, le=5)
    score: float = Field(..., ge=0, le=1)
    reason: str | None = None


class RankingResultRead(RankingResultCreate):
    id: str
    created_at: datetime
    # Prompt 06: the five component scores + total, as persisted
    # on the row (None for rows written before Prompt 06).
    score_breakdown: dict | None = None


# ---------------------------------------------------------------- storyboard / narration / EDL
class StoryboardScene(BaseModel):
    scene_number: int = Field(..., ge=1)
    rank: int | None = Field(default=None, ge=1, le=5)
    moment_id: str | None = None
    narration: str | None = None
    duration_seconds: float | None = Field(default=None, gt=0)


class StoryboardCreate(BaseModel):
    job_id: str
    title: str | None = None
    hook: str | None = None
    narration_script: str | None = None
    scenes: list[StoryboardScene] = Field(default_factory=list)


class StoryboardRead(StoryboardCreate):
    id: str
    created_at: datetime


class NarrationCreate(BaseModel):
    job_id: str
    storyboard_id: str | None = None
    script_text: str = Field(..., min_length=1)
    audio_path: str | None = None
    duration_seconds: float | None = Field(default=None, gt=0)
    status: Literal["draft", "generated", "approved"] = "draft"


class NarrationRead(NarrationCreate):
    id: str
    created_at: datetime


class EDLClip(BaseModel):
    clip_number: int = Field(..., ge=1)
    moment_id: str
    source_path: str
    start_seconds: float = Field(..., ge=0)
    end_seconds: float = Field(..., gt=0)
    caption: str | None = None


class EditDecisionList(BaseModel):
    """Edit Decision List handed to the production agent."""

    job_id: str
    storyboard_id: str | None = None
    clips: list[EDLClip] = Field(..., min_length=1, max_length=5)
    output_width: int = 1080
    output_height: int = 1920
    fps: int = 30


class EditInstructionRead(BaseModel):
    id: str
    job_id: str
    storyboard_id: str | None = None
    edl: dict | None = None
    output_width: int
    output_height: int
    fps: int
    created_at: datetime


# ---------------------------------------------------------------- rendered video / QA
class RenderedVideoRead(BaseModel):
    id: str
    job_id: str
    edit_instruction_id: str | None = None
    file_path: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    status: Literal["rendering", "ready", "failed"]
    created_at: datetime


class QACheck(BaseModel):
    name: str
    verdict: QAVerdict
    detail: str | None = None


class QAReportCreate(BaseModel):
    job_id: str
    rendered_video_id: str | None = None
    verdict: QAVerdict = "not_tested"
    checks: list[QACheck] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class QAReportRead(QAReportCreate):
    id: str
    created_at: datetime


# ---------------------------------------------------------------- agent logs
class AgentExecutionLogRead(BaseModel):
    id: str
    job_id: str
    agent_name: str
    task_id: str | None = None
    status: AgentLogStatus
    detail: str | None = None
    duration_ms: int | None = None
    created_at: datetime


# ---------------------------------------------------------------- health
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    database_ok: bool
    ffmpeg_ok: bool
    storage_backend: str
