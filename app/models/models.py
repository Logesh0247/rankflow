"""Momentrix AI — SQLAlchemy models (Phase 4, database design).

One model per persistent entity in the brief: jobs, candidate videos,
sources, rights evidence, approved footage, analyzed moments, ranking
results, storyboards, narration, editing instructions, rendered videos,
QA reports and agent execution logs.

Relationships: a Job owns everything produced for one topic.
A CandidateVideo belongs to one Job and one Source. RightsEvidence
belongs to one CandidateVideo. ApprovedFootage, AnalyzedMoment and
RankingResult all trace back to a CandidateVideo and a Job, so any
final clip can be audited back to its source and its rights check.

Status fields are plain strings with a documented vocabulary in
docs/DATABASE.md; CHECK constraints enforce the vocabulary on
PostgreSQL and are recorded in schema.sql. SQLite enforces them
only where the DDL is applied via schema.sql.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('created','running','awaiting_approval','approved','failed','cancelled')", name="ck_jobs_status"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    # Workflow stage (Prompt 02). The stage vocabulary is the
    # orchestrator's, not the JobStatus vocabulary: INITIALIZED,
    # RESEARCH, RIGHTS_VERIFICATION, VIDEO_ANALYSIS, RANKING,
    # CREATIVE_PLANNING, VIDEO_PRODUCTION, QUALITY_ASSURANCE,
    # COMPLETED, FAILED, CANCELLED. A COMPLETED stage maps to job
    # status "awaiting_approval" (the video is ready for the user);
    # there is no JobStatus "completed".
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="INITIALIZED")
    requested_by: Mapped[str] = mapped_column(String(120), nullable=False, default="user")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    candidates: Mapped[list["CandidateVideo"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    moments: Mapped[list["AnalyzedMoment"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    ranking_results: Mapped[list["RankingResult"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    storyboards: Mapped[list["Storyboard"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    narrations: Mapped[list["Narration"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    edit_instructions: Mapped[list["EditInstruction"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    rendered_videos: Mapped[list["RenderedVideo"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    qa_reports: Mapped[list["QAReport"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    agent_logs: Mapped[list["AgentExecutionLog"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    cost_events: Mapped[list["CostEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Source(Base):
    """A video platform / channel a candidate came from (e.g. YouTube)."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_sources_platform_external"),)


class CandidateVideo(Base):
    __tablename__ = "candidate_videos"
    __table_args__ = (
        CheckConstraint("status IN ('discovered','rights_pending','rights_approved','rights_rejected','analyzed','selected','rejected')", name="ck_candidates_status"),
        Index("ix_candidates_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("sources.id"), nullable=False)
    external_video_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    view_count: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="discovered")
    relevance_score: Mapped[float | None] = mapped_column(Float)
    # Prompt 03: provenance the shared CandidateVideoCreate schema did
    # not carry before — creator, description and licence as published
    # by the discovery provider (None = not published, never invented).
    creator: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    licence: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="candidates")
    source: Mapped["Source"] = relationship()
    rights: Mapped[list["RightsEvidence"]] = relationship(back_populates="candidate", cascade="all, delete-orphan")
    moments: Mapped[list["AnalyzedMoment"]] = relationship(back_populates="candidate", cascade="all, delete-orphan")


class RightsEvidence(Base):
    """Evidence a source may be used commercially, with the check that produced it.

    Prompt 04: the full Phase 2 record — licence name/URL, the
    permission evidence, verification date/method, the three
    permission flags, attribution, restrictions, expiry and the
    reviewer. One row per verification; re-verifying a candidate
    appends a new row (the gate reads the LATEST one), unless
    nothing changed. Verdict vocabulary: approved, rejected,
    needs_review, expired — never "approved" without evidence.
    """

    __tablename__ = "rights_evidence"
    __table_args__ = (
        CheckConstraint("verdict IN ('approved','rejected','needs_review','expired','not_checked')", name="ck_rights_verdict"),
        Index("ix_rights_candidate", "candidate_video_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    candidate_video_id: Mapped[str] = mapped_column(String(36), ForeignKey("candidate_videos.id", ondelete="CASCADE"), nullable=False)
    licence: Mapped[str | None] = mapped_column(String(255))
    verdict: Mapped[str] = mapped_column(String(32), nullable=False, default="not_checked")
    evidence_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    checked_by: Mapped[str] = mapped_column(String(64), nullable=False, default="rights_agent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    # Prompt 04, Phase 2 — added by migration 004 on pre-existing DBs.
    license_name: Mapped[str | None] = mapped_column(String(255))
    license_url: Mapped[str | None] = mapped_column(Text)
    permission_evidence: Mapped[str | None] = mapped_column(Text)
    verification_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commercial_use: Mapped[bool | None] = mapped_column(Boolean)
    modification_allowed: Mapped[bool | None] = mapped_column(Boolean)
    redistribution_allowed: Mapped[bool | None] = mapped_column(Boolean)
    attribution_required: Mapped[bool | None] = mapped_column(Boolean)
    attribution_text: Mapped[str | None] = mapped_column(Text)
    geographic_restrictions: Mapped[str | None] = mapped_column(Text)
    time_restrictions: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_method: Mapped[str | None] = mapped_column(String(64))
    reviewer_status: Mapped[str | None] = mapped_column(String(32))
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reasons: Mapped[list | None] = mapped_column(JSON)

    candidate: Mapped["CandidateVideo"] = relationship(back_populates="rights")


class ApprovedFootage(Base):
    __tablename__ = "approved_footage"
    __table_args__ = (Index("ix_footage_job", "job_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    candidate_video_id: Mapped[str] = mapped_column(String(36), ForeignKey("candidate_videos.id"), nullable=False)
    rights_evidence_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("rights_evidence.id"))
    storage_path: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class AnalyzedMoment(Base):
    __tablename__ = "analyzed_moments"
    __table_args__ = (Index("ix_moments_job_candidate", "job_id", "candidate_video_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    candidate_video_id: Mapped[str] = mapped_column(String(36), ForeignKey("candidate_videos.id", ondelete="CASCADE"), nullable=False)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    labels: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="moments")
    candidate: Mapped["CandidateVideo"] = relationship(back_populates="moments")


class RankingResult(Base):
    __tablename__ = "ranking_results"
    __table_args__ = (
        CheckConstraint("rank BETWEEN 1 AND 5", name="ck_ranking_rank"),
        UniqueConstraint("job_id", "rank", name="uq_ranking_job_rank"),
        UniqueConstraint("job_id", "moment_id", name="uq_ranking_job_moment"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    moment_id: Mapped[str] = mapped_column(String(36), ForeignKey("analyzed_moments.id"), nullable=False)
    candidate_video_id: Mapped[str] = mapped_column(String(36), ForeignKey("candidate_videos.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    # Prompt 06: the five component scores + total (0-100) that
    # produced `score`, as persisted by the Ranking Agent.
    score_breakdown: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="ranking_results")


class Storyboard(Base):
    __tablename__ = "storyboards"
    __table_args__ = (
        Index("ix_storyboards_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    hook: Mapped[str | None] = mapped_column(Text)
    narration_script: Mapped[str | None] = mapped_column(Text)
    scenes: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="storyboards")


class Narration(Base):
    __tablename__ = "narrations"
    __table_args__ = (
        CheckConstraint("status IN ('draft','generated','approved')", name="ck_narration_status"),
        Index("ix_narrations_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    storyboard_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("storyboards.id"))
    script_text: Mapped[str] = mapped_column(Text, nullable=False)
    audio_path: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="narrations")


class EditInstruction(Base):
    """An Edit Decision List: ordered clip operations for production."""

    __tablename__ = "edit_instructions"
    __table_args__ = (
        Index("ix_edit_instructions_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    storyboard_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("storyboards.id"))
    edl: Mapped[dict | None] = mapped_column(JSON)
    output_width: Mapped[int] = mapped_column(Integer, nullable=False, default=1080)
    output_height: Mapped[int] = mapped_column(Integer, nullable=False, default=1920)
    fps: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="edit_instructions")


class RenderedVideo(Base):
    __tablename__ = "rendered_videos"
    __table_args__ = (
        CheckConstraint("status IN ('rendering','ready','failed')", name="ck_rendered_status"),
        Index("ix_rendered_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    edit_instruction_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("edit_instructions.id"))
    file_path: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="rendering")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="rendered_videos")


class QAReport(Base):
    __tablename__ = "qa_reports"
    __table_args__ = (
        CheckConstraint("verdict IN ('pass','fail','needs_review','not_tested')", name="ck_qa_verdict"),
        Index("ix_qa_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    rendered_video_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("rendered_videos.id"))
    verdict: Mapped[str] = mapped_column(String(32), nullable=False, default="not_tested")
    checks: Mapped[dict | None] = mapped_column(JSON)
    issues: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="qa_reports")


class AgentExecutionLog(Base):
    __tablename__ = "agent_execution_logs"
    __table_args__ = (
        CheckConstraint("status IN ('started','succeeded','failed','skipped','repair_round')", name="ck_agent_log_status"),
        Index("ix_agent_logs_job_agent", "job_id", "agent_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # task_id is "{job_id}:{stage}:{attempt}" (engine.py) — a 36-char
    # UUID + ":" + the longest stage name (RIGHTS_VERIFICATION, 19)
    # + ":" + one attempt digit = 58 chars at most. It MUST stay
    # wide enough for that: String(36) was the 2026-11-21 Neon
    # incident — every job failed at RESEARCH with
    # StringDataRightTruncation because "…:RESEARCH:1" is 47 chars.
    # SQLite never enforced the length, PostgreSQL does. Widened by
    # migration 012 (ALTER COLUMN on existing databases); keep the
    # model, schema.sql and migration 012 in agreement.
    task_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    detail: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="agent_logs")


class CostEvent(Base):
    """One priced unit of work a job consumed (Prompt 12, Phase 3).

    Categories: compute (CPU seconds per stage), external_api
    (provider searches), tts (characters synthesised — zero today:
    no TTS provider is configured). Storage is measured live from
    disk, not stored as events. amount_usd = quantity * unit_cost_usd.
    """

    __tablename__ = "cost_events"
    __table_args__ = (
        Index("ix_cost_events_job", "job_id"),
        CheckConstraint(
            "category IN ('compute','external_api','tts','music_sfx')",
            name="ck_cost_category"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    unit_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="cost_events")


ALL_MODELS = [
    Job, Source, CandidateVideo, RightsEvidence, ApprovedFootage,
    AnalyzedMoment, RankingResult, Storyboard, Narration,
    EditInstruction, RenderedVideo, QAReport, AgentExecutionLog,
    CostEvent,
]
