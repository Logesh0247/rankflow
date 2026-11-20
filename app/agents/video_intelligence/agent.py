"""RANKFLOW AI — Video Intelligence Agent executor (Prompt 05, Phase 6).

The callable the orchestrator registers for the
``video_intelligence`` agent. It takes an OrchestratorTask dict (as
the engine hands it over), runs the VideoIntelligenceService for
that job, and returns a dict the engine validates against
VideoAnalysisOutput (at least one moment id, from at least one
approved candidate). The full report (probe, per-candidate
results, limitations) is persisted in the database — the
orchestrator contract carries only the ids, by design.

Footage files are resolved from ApprovedFootage.storage_path (the
channel a download step will fill in later); a task may also
carry ``footage_paths`` in its input, e.g. {"candidate_id": "/path/to/file.mp4"}.
When no approved candidate has a local file, the service raises —
the engine retries, then fails the job honestly. Unapproved
footage is never analysed: the service calls the Rights gate
before touching any file.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.agents.video_intelligence.service import VideoIntelligenceService
from app.core.config import Settings, get_settings
from app.orchestrator.contracts import OrchestratorTask


def make_video_intelligence_executor(engine: Engine, settings: Settings | None = None):
    """Build the executor the registry registers for agent 'video_intelligence'."""
    settings = settings or get_settings()
    service = VideoIntelligenceService(engine, settings)

    def executor(task: dict) -> dict:
        task_obj = OrchestratorTask.model_validate(task)
        footage_paths = task_obj.input.get("footage_paths")
        if footage_paths is not None and not isinstance(footage_paths, dict):
            raise ValueError("footage_paths must be a mapping of candidate id to file path")
        report = service.run(task_obj.job_id, footage_paths=footage_paths)
        return {"moment_ids": report.moment_ids,
                "candidate_video_ids": report.candidate_video_ids}

    return executor
