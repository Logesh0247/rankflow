"""Creative Director Agent endpoints (Prompt 07).

- GET /jobs/{job_id}/storyboard  — the countdown storyboard (hook + scenes)
- GET /jobs/{job_id}/narration   — the full narration script (draft, no audio yet)
- GET /jobs/{job_id}/edl         — the Edit Decision List for Production
- GET /jobs/{job_id}/creative    — the full creative plan (storyboard + narration + EDL)

Read-only: the plan itself runs through the orchestrator (or
scripts/run_creative_director.py), never from a browser request.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.engine import Engine

from app.agents.creative_director.service import CreativeDirectorService, CreativeReport
from app.core.config import get_settings
from app.core.errors import RankflowError
from app.database.session import get_engine, init_db
from app.schemas.schemas import EditInstructionRead, NarrationRead, StoryboardRead

router = APIRouter(tags=["creative_director"])


def _service() -> CreativeDirectorService:
    engine: Engine = get_engine()
    init_db(engine)
    return CreativeDirectorService(engine, get_settings())


@router.get("/jobs/{job_id}/storyboard", response_model=StoryboardRead)
def get_storyboard(job_id: str) -> StoryboardRead:
    try:
        return _service().get_storyboard(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/narration", response_model=NarrationRead)
def get_narration(job_id: str) -> NarrationRead:
    try:
        return _service().get_narration(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/edl", response_model=EditInstructionRead)
def get_edl(job_id: str) -> EditInstructionRead:
    try:
        return _service().get_edl(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/creative", response_model=CreativeReport)
def get_creative(job_id: str) -> CreativeReport:
    try:
        return _service().get_creative(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
