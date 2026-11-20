"""Video Intelligence Agent endpoints (Prompt 05).

- GET /jobs/{job_id}/moments             — analysed moments for a job, in time order
- GET /jobs/{job_id}/moments/{moment_id} — one moment, with its measured signals
  in `labels` (analyser, confidence, visual quality, context clarity,
  frame references, thumbnail, verification issues).

Read-only: analysis itself runs through the orchestrator (or
scripts/run_video_intelligence.py), never from a browser request.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.engine import Engine

from app.agents.video_intelligence.service import VideoIntelligenceService
from app.core.config import get_settings
from app.core.errors import RankflowError
from app.database.session import init_db, make_engine
from app.schemas.schemas import VideoMomentRead

router = APIRouter(tags=["video_intelligence"])


def _service() -> VideoIntelligenceService:
    engine: Engine = make_engine()
    init_db(engine)
    return VideoIntelligenceService(engine, get_settings())


@router.get("/jobs/{job_id}/moments", response_model=list[VideoMomentRead])
def list_moments(job_id: str) -> list[VideoMomentRead]:
    try:
        return _service().list_moments(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/moments/{moment_id}", response_model=VideoMomentRead)
def get_moment(job_id: str, moment_id: str) -> VideoMomentRead:
    try:
        return _service().get_moment(job_id, moment_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
