"""Orchestrator endpoints (Prompt 02, integrated in Prompt 10).

- GET  /agents                     — the seven registered agents (all real, all ready)
- GET  /agents/{agent_id}/health   — per-agent health
- POST /jobs/{id}/run              — start a job in the background (202; poll /jobs/{id})
- POST /jobs/{id}/resume           — resume a failed job in the background (202); applies
  pending QA repair requests by default, or re-runs from a named
  stage after human intervention
- POST /jobs/{id}/cancel           — cancel a running job
- GET  /jobs/{id}/history          — job history (creation + agent logs)

Run/resume are asynchronous since the 2026-11-21 Render
health-check fix: a job takes many minutes, and running it
inside the HTTP request left /health sharing the process with
the job until Render's 5 s health check timed out and the
service was restarted mid-job, orphaning it. POST /run now
returns 202 immediately with the job as it stands, the job
runs in a background thread (see app/orchestrator/runner.py),
and the UI polls /jobs every 4 s while it runs. A second run
for a job that is already running is refused with 409.

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

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.core.errors import RankflowError, ValidationError
from app.database.session import get_engine, init_db
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.orchestrator.runner import RUNNER
from app.orchestrator.test_doubles import register_test_doubles
from app.schemas.schemas import JobRead
from app.orchestrator.contracts import AgentDescriptor, AgentHealth, JobHistoryEntry

router = APIRouter(tags=["orchestrator"])


def _engine() -> WorkflowEngine:
    engine: Engine = get_engine()
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
    return build_default_registry(get_engine())


def _engine_with_doubles() -> WorkflowEngine:
    engine: Engine = get_engine()
    init_db(engine)
    registry = build_default_registry()
    register_test_doubles(registry)
    return WorkflowEngine(engine, get_settings(), registry)


def _start_background(job_id: str, use_test_doubles: bool,
                      response: Response, resume: bool = False,
                      apply_repairs: bool = True,
                      rerun_from_stage: str | None = None) -> JobRead:
    """Start a job run/resume in a background thread (202), or
    answer synchronously when the job cannot run at all.

    Pre-checks run here, in the request, so a bad call still
    gets its honest status now: unknown job -> 404, terminal
    job -> 200 unchanged (run) / 422 (resume, matching the
    engine), failed job -> 422 for run (resume it instead),
    already running -> 409. The engine itself does the same
    checks inside the thread as a backstop.
    """
    service = JobService(get_engine())
    job = service.get_job(job_id)  # 404 when unknown
    if job.status in ("awaiting_approval", "approved", "cancelled"):
        if resume:
            raise HTTPException(
                status_code=422,
                detail=f"job {job_id} is {job.status} and cannot be resumed")
        return job
    if job.status == "failed" and not resume:
        raise HTTPException(
            status_code=422,
            detail=f"job {job_id} failed; resume it with resume_job() "
                   "instead of re-running it")
    if RUNNER.is_running(job_id):
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id} is already running")

    def _target() -> None:
        engine = _engine_with_doubles() if use_test_doubles else _engine()
        if resume:
            engine.resume_job(job_id, apply_repairs=apply_repairs,
                              rerun_from_stage=rerun_from_stage)
        else:
            engine.run_job(job_id)

    def _fail_job(message: str) -> None:
        JobService(get_engine()).fail_job(job_id, message)

    if not RUNNER.start(job_id, _target, _fail_job):
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id} is already running")
    response.status_code = 202
    return service.get_job(job_id)


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
def run_job(job_id: str, response: Response,
            use_test_doubles: bool = False) -> JobRead:
    """Start a job run in the background: 202 with the job as it
    stands; poll GET /jobs/{job_id} for progress (the UI polls
    every 4 s while it runs). A job that already reached a
    terminal stage is returned unchanged with 200."""
    try:
        return _start_background(job_id, use_test_doubles, response)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/jobs/{job_id}/resume", response_model=JobRead)
def resume_job(job_id: str, response: Response,
               use_test_doubles: bool = False,
               apply_repairs: bool = True,
               rerun_from_stage: str | None = None) -> JobRead:
    """Resume a failed job in the background (202; poll
    GET /jobs/{job_id}). By default applies pending QA repair
    requests (re-runs the owning stage, capped at
    `qa_max_repair_attempts` repair rounds); `rerun_from_stage` re-runs
    from a named stage after human intervention (e.g. more
    candidates approved). Pass apply_repairs=false to resume
    without touching checkpoints."""
    try:
        return _start_background(job_id, use_test_doubles, response,
                                 resume=True, apply_repairs=apply_repairs,
                                 rerun_from_stage=rerun_from_stage)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/jobs/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: str) -> JobRead:
    service = JobService(get_engine())
    try:
        return service.cancel_job(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/history", response_model=list[JobHistoryEntry])
def job_history(job_id: str) -> list[JobHistoryEntry]:
    service = JobService(get_engine())
    try:
        return service.job_history(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
