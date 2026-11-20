"""RANKFLOW AI — Research Agent executor (Prompt 03, Phase 6).

The callable the orchestrator registers for the ``research`` agent.
It takes an OrchestratorTask dict (as the engine hands it over),
runs the ResearchService for that job, and returns a dict the
engine validates against ResearchOutput (at least one candidate
id). The full report (plan, scores, provider outcomes, limitations)
is persisted in the database and in the execution log — the
orchestrator contract carries only the ids, by design.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.agents.research.service import ResearchService
from app.core.config import Settings, get_settings
from app.orchestrator.contracts import OrchestratorTask
from app.orchestrator.jobs import JobService


def make_research_executor(engine: Engine, settings: Settings | None = None):
    """Build the executor the registry registers for agent 'research'."""
    settings = settings or get_settings()
    service = ResearchService(engine, settings)
    jobs = JobService(engine)

    def executor(task: dict) -> dict:
        task_obj = OrchestratorTask.model_validate(task)
        topic = task_obj.input.get("topic")
        if not topic:
            topic = jobs.get_job(task_obj.job_id).topic
        report = service.run(task_obj.job_id, topic)
        ids = [c.id for c in report.candidates]
        return {"candidate_ids": ids, "candidate_video_ids": ids}

    return executor
