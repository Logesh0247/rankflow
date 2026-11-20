"""RANKFLOW AI — Video Production Agent executor (Prompt 08, Phase 8).

The callable the orchestrator registers for the ``production``
agent. It takes an OrchestratorTask dict, runs the
ProductionService for that job, and returns a dict the engine
validates against ProductionOutput (rendered video id, file
path, duration, 1080x1920). The full render (segments, manifest,
validation) is persisted — the orchestrator contract carries only
the ids and measurements, by design.

When the EDL is missing, a clip is unapproved, footage is missing
or the render fails validation, the service raises — the engine
retries, then fails the job honestly. No video is faked to fill
the stage.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.agents.production.service import ProductionService
from app.core.config import Settings, get_settings
from app.orchestrator.contracts import OrchestratorTask


def make_production_executor(engine: Engine, settings: Settings | None = None):
    """Build the executor the registry registers for agent 'production'."""
    settings = settings or get_settings()
    service = ProductionService(engine, settings)

    def executor(task: dict) -> dict:
        task_obj = OrchestratorTask.model_validate(task)
        report = service.run(task_obj.job_id)
        return {"rendered_video_id": report.rendered_video_id,
                "file_path": report.file_path,
                "duration_seconds": report.duration_seconds,
                "width": report.width,
                "height": report.height}

    return executor
