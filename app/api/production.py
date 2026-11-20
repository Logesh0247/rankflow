"""Video Production Agent endpoints (Prompt 08).

- GET /jobs/{job_id}/rendered-video          — the rendered video record
- GET /jobs/{job_id}/production              — the full render report / manifest
- GET /jobs/{job_id}/rendered-video/download — the MP4 file itself

Read-only: the render itself runs through the orchestrator (or
scripts/run_production.py), never from a browser request. The
download serves only files under the configured storage root.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.engine import Engine

from app.agents.production.service import ProductionReport, ProductionService
from app.core.config import get_settings
from app.core.errors import RankflowError
from app.database.session import init_db, make_engine
from app.schemas.schemas import RenderedVideoRead

router = APIRouter(tags=["production"])


def _service() -> ProductionService:
    engine: Engine = make_engine()
    init_db(engine)
    return ProductionService(engine, get_settings())


@router.get("/jobs/{job_id}/rendered-video", response_model=RenderedVideoRead)
def get_rendered_video(job_id: str) -> RenderedVideoRead:
    try:
        return _service().get_rendered_video(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/production", response_model=ProductionReport)
def get_production(job_id: str) -> ProductionReport:
    try:
        return _service().get_production(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/rendered-video/download")
def download_rendered_video(job_id: str) -> FileResponse:
    try:
        rendered = _service().get_rendered_video(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if not rendered.file_path:
        raise HTTPException(status_code=404, detail="no rendered video file for this job")
    path = Path(rendered.file_path)
    root = Path(get_settings().storage_root).resolve()
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="rendered video is outside storage") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="rendered video file not found")
    return FileResponse(path, media_type="video/mp4",
                        filename=f"rankflow-{job_id}.mp4")
