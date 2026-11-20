"""Orchestrator endpoints (Prompt 02, integrated in Prompt 10).

- GET  /agents                     — the seven registered agents (all real, all ready)
- GET  /agents/{agent_id}/health   — per-agent health
- POST /jobs/{id}/run              — run a job through the workflow (topic only — autonomous)
- POST /jobs/{id}/resume           — resume a failed job; applies pending QA repair
  requests by default, or re-runs from a named stage after human intervention
- POST /jobs/{id}/cancel           — cancel a running job
- GET  /jobs/{id}/history          — job history (creation + agent logs)

All seven agents are real (Prompts 03-09) and registered, so a plain
run goes research -> rights -> video intelligence -> ranking ->
creative director -> production -> quality assurance ->
awaiting_approval when five eligible sources with analysed moments
exist; otherwise the job fails honestly at the stage that cannot
proceed. Pass ?use_test_doubles=true to run the explicitly test-only
doubles instead — that is only for orchestrator testing, and the
response says so.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.core.errors import RankflowError
from app.database.session import init_db, make_engine
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.orchestrator.test_doubles import register_test_doubles
from app.schemas.schemas import JobRead
from app.orchestrator.contracts import AgentDescriptor, AgentHealth, JobHistoryEntry

router = APIRouter(tags=["orchestrator"])


def _engine() -> WorkflowEngine:
    engine: Engine = make_engine()
    init_db(engine)
    # Engine-backed registry: all seven real agents (Prompts 03-09)
    # are registered, so a plain run goes end to end — provided at
    # least five candidates are approved with local footage;
    # otherwise the job fails honestly at the stage that cannot
    # proceed. No test doubles are used.
    return WorkflowEngine(engine, get_settings(), build_default_registry(engine))


def _registry():
    """Registry for /agents listing and health: all seven agents
    show 'ready' (Prompts 03-09)."""
    return build_default_registry(make_engine())


def _engine_with_doubles() -> WorkflowEngine:
    engine: Engine = make_engine()
    init_db(engine)
    registry = build_default_registry()
    register_test_doubles(registry)
    return WorkflowEngine(engine, get_settings(), registry)


@router.get("/agents", response_model=list[AgentDescriptor])
def list_agents() -> list[AgentDescriptor]:
    return _registry().list()


@router.get("/agents/{agent_id}/health", response_model=AgentHealth)
def agent_health(agent_id: str) -> AgentHealth:
    try:
        return _registry().health(agent_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/jobs/{job_id}/run", response_model=JobRead)
def run_job(job_id: str, use_test_doubles: bool = False) -> JobRead:
    engine = _engine_with_doubles() if use_test_doubles else _engine()
    try:
        return engine.run_job(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/jobs/{job_id}/resume", response_model=JobRead)
def resume_job(job_id: str, use_test_doubles: bool = False,
               apply_repairs: bool = True,
               rerun_from_stage: str | None = None) -> JobRead:
    """Resume a failed job. By default applies pending QA repair
    requests (re-runs the owning stage, capped at
    `qa_max_repair_attempts` rounds); `rerun_from_stage` re-runs
    from a named stage after human intervention (e.g. more
    candidates approved). Pass apply_repairs=false to resume
    without touching checkpoints."""
    engine = _engine_with_doubles() if use_test_doubles else _engine()
    try:
        return engine.resume_job(job_id, apply_repairs=apply_repairs,
                                 rerun_from_stage=rerun_from_stage)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/jobs/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: str) -> JobRead:
    service = JobService(make_engine())
    try:
        return service.cancel_job(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/history", response_model=list[JobHistoryEntry])
def job_history(job_id: str) -> list[JobHistoryEntry]:
    service = JobService(make_engine())
    try:
        return service.job_history(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
