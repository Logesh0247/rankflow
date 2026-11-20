"""RANKFLOW AI — Rights Verification Agent executor (Prompt 04, Phase 6).

The callable the orchestrator registers for the ``rights`` agent.
It takes an OrchestratorTask dict (as the engine hands it over),
runs the RightsService for that job, and returns a dict the engine
validates against RightsOutput (at least one approved candidate
id). The full report (per-candidate verdicts, evidence, reasons,
limitations) is persisted in the database — the orchestrator
contract carries only the ids, by design.

When no candidate can be approved, the service raises (never
returns an empty approved list): the engine retries the stage,
then fails the job with the reason stored. An unverified source
is never approved merely to keep production moving.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.agents.rights.service import RightsService
from app.core.config import Settings, get_settings
from app.orchestrator.contracts import OrchestratorTask


def make_rights_executor(engine: Engine, settings: Settings | None = None):
    """Build the executor the registry registers for agent 'rights'."""
    settings = settings or get_settings()
    service = RightsService(engine, settings)

    def executor(task: dict) -> dict:
        task_obj = OrchestratorTask.model_validate(task)
        report = service.run(task_obj.job_id)
        approved = [v.candidate_video_id for v in report.approved]
        rejected = [v.candidate_video_id for v in report.rejected]
        return {"approved_candidate_ids": approved,
                "rejected_candidate_ids": rejected}

    return executor
