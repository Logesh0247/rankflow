"""RANKFLOW AI — workflow states (Prompt 02, Phase 3).

The workflow is a strict linear sequence. A stage may only run
once every earlier stage has a persisted successful checkpoint.
Terminal states are COMPLETED, FAILED and CANCELLED.
"""
from __future__ import annotations

from enum import Enum


class WorkflowStage(str, Enum):
    INITIALIZED = "INITIALIZED"
    RESEARCH = "RESEARCH"
    RIGHTS_VERIFICATION = "RIGHTS_VERIFICATION"
    VIDEO_ANALYSIS = "VIDEO_ANALYSIS"
    RANKING = "RANKING"
    CREATIVE_PLANNING = "CREATIVE_PLANNING"
    VIDEO_PRODUCTION = "VIDEO_PRODUCTION"
    QUALITY_ASSURANCE = "QUALITY_ASSURANCE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# The seven agent stages, in execution order. INITIALIZED is the
# starting state, COMPLETED/FAILED/CANCELLED are terminal.
AGENT_STAGES: list[WorkflowStage] = [
    WorkflowStage.RESEARCH,
    WorkflowStage.RIGHTS_VERIFICATION,
    WorkflowStage.VIDEO_ANALYSIS,
    WorkflowStage.RANKING,
    WorkflowStage.CREATIVE_PLANNING,
    WorkflowStage.VIDEO_PRODUCTION,
    WorkflowStage.QUALITY_ASSURANCE,
]

TERMINAL_STAGES: frozenset[WorkflowStage] = frozenset(
    {WorkflowStage.COMPLETED, WorkflowStage.FAILED, WorkflowStage.CANCELLED}
)

# Stage -> agent id (see app/orchestrator/registry.py).
STAGE_AGENT: dict[WorkflowStage, str] = {
    WorkflowStage.RESEARCH: "research",
    WorkflowStage.RIGHTS_VERIFICATION: "rights",
    WorkflowStage.VIDEO_ANALYSIS: "video_intelligence",
    WorkflowStage.RANKING: "ranking",
    WorkflowStage.CREATIVE_PLANNING: "creative_director",
    WorkflowStage.VIDEO_PRODUCTION: "production",
    WorkflowStage.QUALITY_ASSURANCE: "quality_assurance",
}

# Workflow stage -> Job.status (Prompt 01 vocabulary). COMPLETED
# means "video rendered and QA'd, ready for the user", which is
# "awaiting_approval" in the Job vocabulary.
STAGE_JOB_STATUS: dict[WorkflowStage, str] = {
    WorkflowStage.INITIALIZED: "created",
    WorkflowStage.RESEARCH: "running",
    WorkflowStage.RIGHTS_VERIFICATION: "running",
    WorkflowStage.VIDEO_ANALYSIS: "running",
    WorkflowStage.RANKING: "running",
    WorkflowStage.CREATIVE_PLANNING: "running",
    WorkflowStage.VIDEO_PRODUCTION: "running",
    WorkflowStage.QUALITY_ASSURANCE: "running",
    WorkflowStage.COMPLETED: "awaiting_approval",
    WorkflowStage.FAILED: "failed",
    WorkflowStage.CANCELLED: "cancelled",
}


def next_stage(stage: WorkflowStage) -> WorkflowStage | None:
    """Stage after `stage`, or None if `stage` is terminal."""
    if stage == WorkflowStage.INITIALIZED:
        return AGENT_STAGES[0]
    if stage in AGENT_STAGES:
        idx = AGENT_STAGES.index(stage)
        if idx + 1 < len(AGENT_STAGES):
            return AGENT_STAGES[idx + 1]
        return WorkflowStage.COMPLETED
    return None


def dependencies_satisfied(stage: WorkflowStage, completed: set[WorkflowStage]) -> bool:
    """True only if every earlier agent stage is in `completed`."""
    if stage == WorkflowStage.INITIALIZED:
        return True
    if stage not in AGENT_STAGES:
        return False
    idx = AGENT_STAGES.index(stage)
    return all(s in completed for s in AGENT_STAGES[:idx])
