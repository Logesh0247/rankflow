"""RANKFLOW AI — Quality Assurance Agent executor (Prompt 09, Phase 7).

The callable the orchestrator registers for the ``quality_assurance``
agent. It takes an OrchestratorTask dict, runs the QAService for
that job, and returns a dict the engine validates against
QAOutput (verdict + issues). The full report (all five
validations, checks, repair requests, recommendations) is
persisted as a QAReport row.

A FAILED verdict is raised, not returned as a success: the
orchestrator retries, then fails the job, so a defective video
is never presented for user approval. A NEEDS_REVIEW verdict
(narration voice never generated, audio effectively silent, a
check that could not be verified) is returned normally — the
job completes to awaiting_approval with the QA report flagging
exactly what is missing, and the user decides.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.agents.quality_assurance.service import QAService
from app.core.config import Settings, get_settings
from app.core.errors import DeterministicError
from app.orchestrator.contracts import OrchestratorTask


def make_quality_assurance_executor(engine: Engine, settings: Settings | None = None):
    """Build the executor the registry registers for agent 'quality_assurance'."""
    settings = settings or get_settings()
    service = QAService(engine, settings)

    def executor(task: dict) -> dict:
        task_obj = OrchestratorTask.model_validate(task)
        report = service.run(task_obj.job_id)
        if report.verdict == "fail":
            # Deterministic (Prompt 10): re-inspecting the same
            # render always fails the same way, so the orchestrator
            # must not retry this stage. Recovery is via resume,
            # which applies the repair requests and re-runs the
            # owning stage, or via human intervention.
            raise DeterministicError(
                f"Quality Assurance FAILED job {task_obj.job_id!r}: "
                + "; ".join(report.issues)
                + f" ({len(report.repair_requests)} repair request(s) — "
                "resume the job to apply them)")
        return {"verdict": report.verdict, "issues": report.issues}

    return executor
