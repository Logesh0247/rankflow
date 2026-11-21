"""Momentrix AI — Ranking Agent executor (Prompt 06, Phase 6).

The callable the orchestrator registers for the ``ranking``
agent. It takes an OrchestratorTask dict (as the engine hands
it over), runs the RankingService for that job, and returns a
dict the engine validates against RankingOutput (exactly five
ranks, 1-5, each from a different source video). The full
report (component scores, duplicates dropped, counts,
limitations) is persisted in the database rows — the
orchestrator contract carries only the five ranked entries, by
design.

When fewer than five eligible sources exist the service raises
— the engine retries, then fails the job honestly. No moment
is invented to fill the five, and rights approval is never
bypassed.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.agents.ranking.service import RankingService
from app.core.config import Settings, get_settings
from app.orchestrator.contracts import OrchestratorTask


def make_ranking_executor(engine: Engine, settings: Settings | None = None):
    """Build the executor the registry registers for agent 'ranking'."""
    settings = settings or get_settings()
    service = RankingService(engine, settings)

    def executor(task: dict) -> dict:
        task_obj = OrchestratorTask.model_validate(task)
        report = service.run(task_obj.job_id)
        return {"ranks": [
            {"rank": entry.rank, "moment_id": entry.moment_id,
             "candidate_video_id": entry.candidate_video_id,
             "score": entry.score}
            for entry in report.ranks]}

    return executor
