"""Cost monitoring endpoints (Prompt 12, Phase 3).

- GET /jobs/{job_id}/cost — measured compute seconds, API calls,
  TTS usage, live storage size and the estimated dollars for one job.
- GET /cost/summary — totals across all jobs plus the rates used.

All dollars are estimates from measured usage and the documented
rates in app/core/cost.py. No secret or key is ever in a response.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.engine import Engine

from app.core.cost import CostService, JobCostSummary
from app.core.errors import RankflowError
from app.database.session import init_db, make_engine
from app.orchestrator.jobs import JobService

router = APIRouter(tags=["cost"])


def _service() -> CostService:
    engine: Engine = make_engine()
    init_db(engine)
    return CostService(engine)


@router.get("/jobs/{job_id}/cost", response_model=JobCostSummary)
def get_job_cost(job_id: str) -> JobCostSummary:
    try:
        JobService(make_engine()).get_job(job_id)
        return _service().job_cost(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/cost/summary")
def get_cost_summary() -> dict:
    return _service().summary()
