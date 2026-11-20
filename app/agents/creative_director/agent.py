"""RANKFLOW AI — Creative Director Agent executor (Prompt 07, Phase 6).

The callable the orchestrator registers for the
``creative_director`` agent. It takes an OrchestratorTask dict (as
the engine hands it over), runs the CreativeDirectorService for
that job, and returns a dict the engine validates against
CreativeOutput (storyboard id, scene count, exactly the five
ranked moment ids). The full plan (hook, storyboard, narration,
caption plan, visual direction, EDL) is persisted in the
database — the orchestrator contract carries only the ids, by
design.

When fewer than five moments are ranked, or a ranked candidate
is no longer Rights-approved, the service raises — the engine
retries, then fails the job honestly. No moment is invented to
fill the countdown, and rights approval is never bypassed.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.agents.creative_director.service import CreativeDirectorService
from app.core.config import Settings, get_settings
from app.orchestrator.contracts import OrchestratorTask


def make_creative_director_executor(engine: Engine, settings: Settings | None = None):
    """Build the executor the registry registers for agent 'creative_director'."""
    settings = settings or get_settings()
    service = CreativeDirectorService(engine, settings)

    def executor(task: dict) -> dict:
        task_obj = OrchestratorTask.model_validate(task)
        report = service.run(task_obj.job_id)
        return {"storyboard_id": report.storyboard_id,
                "scene_count": len(report.scenes),
                "ranked_moment_ids": [r["moment_id"] for r in report.edl["clips"]]}

    return executor
