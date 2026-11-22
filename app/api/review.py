"""Review endpoints (Prompt 11 — Final Video Review).

- POST /jobs/{job_id}/approve — approve a completed video. Only a
  job in `awaiting_approval` can be approved.
- POST /jobs/{job_id}/reject  — reject the completed video and
  regenerate it: Production re-renders from the approved EDL and
  QA re-inspects the new render. Only a job in
  `awaiting_approval` can be rejected; earlier stages keep their
  checkpoints, so only production + QA re-run. A user rejection is
  not a QA repair round and does not consume the repair cap.
  The call runs the re-render synchronously (renders are slow on
  CPU) — the UI polls GET /jobs/{id} while it runs.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.core.errors import RankflowError
from app.database.session import get_engine, init_db
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.schemas.schemas import JobRead

router = APIRouter(tags=["review"])


def _engine() -> WorkflowEngine:
    engine: Engine = get_engine()
    init_db(engine)
    return WorkflowEngine(engine, get_settings(), build_default_registry(engine))


@router.post("/jobs/{job_id}/approve", response_model=JobRead)
def approve_job(job_id: str) -> JobRead:
    service = JobService(get_engine())
    try:
        return service.approve_job(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/jobs/{job_id}/reject", response_model=JobRead)
def reject_job(job_id: str) -> JobRead:
    try:
        return _engine().regenerate_job(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
