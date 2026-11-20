"""RANKFLOW AI — orchestrator communication contracts (Prompt 02, Phase 4).

Every task the orchestrator hands to an agent, and every response
an agent returns, is one of these Pydantic models. The stage output
models at the bottom are the per-stage output schemas the registry
advertises and the engine validates against — an agent whose output
does not validate is treated as failed, never silently accepted.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- tasks / responses
class OrchestratorTask(BaseModel):
    """A unit of work handed to one agent (Phase 4: task contract)."""

    task_id: str
    job_id: str
    agent_id: str = Field(..., pattern=r"^[a-z_]+$")
    input_references: dict[str, str | list[str]] = Field(default_factory=dict)
    expected_output_schema: str
    deadline: datetime
    retry_count: int = Field(default=0, ge=0)
    attempt: int = Field(default=1, ge=1)
    # Free-form input the agent needs (topic, prior-stage outputs).
    input: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _retry_matches_attempt(self) -> "OrchestratorTask":
        if self.retry_count != self.attempt - 1:
            raise ValueError("retry_count must equal attempt - 1")
        return self


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class OrchestratorResponse(BaseModel):
    """What an agent returns (Phase 4: response contract)."""

    task_id: str
    job_id: str
    agent_id: str
    status: Literal["succeeded", "failed", "invalid"]
    output_references: dict[str, str | list[str]] = Field(default_factory=dict)
    validation: ValidationResult = Field(default_factory=lambda: ValidationResult(valid=True))
    errors: list[str] = Field(default_factory=list)
    execution_metadata: dict[str, Any] = Field(default_factory=dict)


class AgentHealth(BaseModel):
    agent_id: str
    status: Literal["not_implemented", "test_double", "ready", "unhealthy"]
    description: str
    detail: str = ""


class AgentDescriptor(BaseModel):
    agent_id: str
    name: str
    description: str
    input_schema: str
    output_schema: str
    execution_method: str
    status: Literal["not_implemented", "test_double", "ready"]
    health: AgentHealth


class JobHistoryEntry(BaseModel):
    timestamp: datetime
    event: str
    stage: str | None = None
    status: str
    detail: str | None = None


# ---------------------------------------------------------------- per-stage output schemas
class ResearchOutput(BaseModel):
    candidate_ids: list[str] = Field(..., min_length=1)
    candidate_video_ids: list[str] = Field(default_factory=list)


class RightsOutput(BaseModel):
    approved_candidate_ids: list[str] = Field(..., min_length=1)
    rejected_candidate_ids: list[str] = Field(default_factory=list)


class VideoAnalysisOutput(BaseModel):
    moment_ids: list[str] = Field(..., min_length=1)
    candidate_video_ids: list[str] = Field(..., min_length=1)


class RankingEntry(BaseModel):
    rank: int = Field(..., ge=1, le=5)
    moment_id: str
    candidate_video_id: str
    score: float = Field(..., ge=0, le=1)


class RankingOutput(BaseModel):
    """Exactly five ranked moments, each from a different source video."""

    ranks: list[RankingEntry] = Field(..., min_length=5, max_length=5)

    @model_validator(mode="after")
    def _five_unique_sources(self) -> "RankingOutput":
        ranks = sorted(r.rank for r in self.ranks)
        if ranks != [1, 2, 3, 4, 5]:
            raise ValueError("ranks must be exactly 1-5, one each")
        sources = [r.candidate_video_id for r in self.ranks]
        if len(set(sources)) != 5:
            raise ValueError("ranked moments must come from five different source videos")
        moments = [r.moment_id for r in self.ranks]
        if len(set(moments)) != 5:
            raise ValueError("ranked moments must be five different moments")
        return self


class CreativeOutput(BaseModel):
    storyboard_id: str
    scene_count: int = Field(..., ge=1)
    ranked_moment_ids: list[str] = Field(..., min_length=5, max_length=5)


class ProductionOutput(BaseModel):
    rendered_video_id: str
    file_path: str
    duration_seconds: float = Field(..., gt=0)
    width: int = 1080
    height: int = 1920


class QAOutput(BaseModel):
    verdict: Literal["pass", "fail", "needs_review"]
    issues: list[str] = Field(default_factory=list)


# Stage -> output schema model, used by the registry and the engine.
STAGE_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "RESEARCH": ResearchOutput,
    "RIGHTS_VERIFICATION": RightsOutput,
    "VIDEO_ANALYSIS": VideoAnalysisOutput,
    "RANKING": RankingOutput,
    "CREATIVE_PLANNING": CreativeOutput,
    "VIDEO_PRODUCTION": ProductionOutput,
    "QUALITY_ASSURANCE": QAOutput,
}
