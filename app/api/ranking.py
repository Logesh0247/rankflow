"""Ranking Agent endpoints (Prompt 06).

- GET /jobs/{job_id}/ranking          — the five ranked moments, #1 first
- GET /jobs/{job_id}/ranking/{rank}   — one rank (1-5), with its component
  scores and explanation

Read-only: ranking itself runs through the orchestrator (or
scripts/run_ranking.py), never from a browser request.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.engine import Engine

from app.agents.ranking.service import RankingService
from app.core.config import get_settings
from app.core.errors import RankflowError
from app.database.session import init_db, make_engine
from app.schemas.schemas import RankingResultRead

router = APIRouter(tags=["ranking"])


def _service() -> RankingService:
    engine: Engine = make_engine()
    init_db(engine)
    return RankingService(engine, get_settings())


@router.get("/jobs/{job_id}/ranking", response_model=list[RankingResultRead])
def list_ranking(job_id: str) -> list[RankingResultRead]:
    try:
        return _service().list_ranking_reads(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/ranking/{rank}", response_model=RankingResultRead)
def get_rank(job_id: str, rank: int) -> RankingResultRead:
    try:
        return _service().get_rank_read(job_id, rank)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
