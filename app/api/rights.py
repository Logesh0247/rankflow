"""Rights Verification Agent endpoints (Prompt 04).

- GET  /jobs/{job_id}/rights                        — latest rights evidence per candidate
- GET  /jobs/{job_id}/rights/{candidate_id}         — latest evidence for one candidate
- POST /jobs/{job_id}/rights/{candidate_id}/review  — human review: supply evidence,
                                                       the agent re-evaluates (it cannot
                                                       be forced to approve)
- GET  /jobs/{job_id}/attribution                  — attribution for APPROVED candidates
                                                       only, for Creative Director / Production
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.engine import Engine

from app.agents.rights.evidence import ManualEvidence
from app.agents.rights.service import RightsService
from app.core.config import get_settings
from app.core.errors import RankflowError, ValidationError
from app.database.session import get_engine, init_db
from app.schemas.schemas import (
    AttributionRead,
    RightsReviewRequest,
    RightsVerificationRead,
)

router = APIRouter(tags=["rights"])


def _service() -> RightsService:
    engine: Engine = get_engine()
    init_db(engine)
    return RightsService(engine, get_settings())


@router.get("/jobs/{job_id}/rights", response_model=list[RightsVerificationRead])
def list_rights(job_id: str) -> list[RightsVerificationRead]:
    try:
        return _service().list_rights(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/rights/{candidate_id}", response_model=RightsVerificationRead)
def get_rights(job_id: str, candidate_id: str) -> RightsVerificationRead:
    try:
        return _service().get_rights(job_id, candidate_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/jobs/{job_id}/rights/{candidate_id}/review",
             response_model=RightsVerificationRead)
def review_rights(job_id: str, candidate_id: str,
                  payload: RightsReviewRequest) -> RightsVerificationRead:
    try:
        manual = ManualEvidence(
            licence_name=payload.licence_name,
            licence_url=payload.licence_url,
            permission_evidence=payload.permission_evidence,
            permission_granted=payload.permission_granted,
            expires_at=payload.expires_at,
            geographic_restrictions=payload.geographic_restrictions,
            time_restrictions=payload.time_restrictions,
            notes=payload.notes,
            reviewed_by=payload.reviewer,
        )
        return _service().review_candidate(job_id, candidate_id, manual)
    except ValueError as exc:
        # e.g. permission_granted asserted without any evidence.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/jobs/{job_id}/attribution", response_model=list[AttributionRead])
def list_attribution(job_id: str) -> list[AttributionRead]:
    try:
        return _service().list_attribution(job_id)
    except RankflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
