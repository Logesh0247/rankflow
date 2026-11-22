"""Quality Assurance Agent endpoints (Prompt 09).

- POST /jobs/{job_id}/qa/run  — run QA on the job's rendered video
- GET  /jobs/{job_id}/qa       — the latest QA report

Read-only except for the explicit run: QA itself runs through
the orchestrator (or scripts/run_qa.py), never from a browser
request. A FAILED verdict is returned as HTTP 422 with the
issues — the video must not be presented for approval.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.engine import Engine

from app.agents.quality_assurance.service import QAReportFull, QAService
from app.core.config import get_settings
from app.core.errors import ExternalServiceError, RankflowError
from app.database.session import get_engine, init_db

router = APIRouter(tags=["quality_assurance"])


def _service() -> QAService:
    engine: Engine = get_engine()
    init_db(engine)
    return QAService(engine, get_settings())


@router.post("/jobs/{job_id}/qa/run", response_model=QAReportFull)
def run_qa(job_id: str) -> QAReportFull:
    try:
        report = _service().run(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if report.verdict == "fail":
        raise HTTPException(status_code=422, detail="; ".join(report.issues))
    return report


@router.get("/jobs/{job_id}/qa", response_model=QAReportFull)
def get_qa(job_id: str) -> QAReportFull:
    try:
        return _service().get_report(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/qa/history", response_model=list[QAReportFull])
def list_qa(job_id: str) -> list[QAReportFull]:
    try:
        return _service().list_reports(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
