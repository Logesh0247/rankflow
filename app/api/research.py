"""Research Agent endpoints (Prompt 03).

- GET /jobs/{job_id}/candidates — candidates the Research Agent
  persisted for a job, best first (relevance_score desc).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import sessionmaker

from app.core.errors import RankflowError
from app.database.session import get_engine
from app.models.models import CandidateVideo
from app.schemas.schemas import CandidateVideoRead

router = APIRouter(tags=["research"])


@router.get("/jobs/{job_id}/candidates", response_model=list[CandidateVideoRead])
def list_candidates(job_id: str) -> list[CandidateVideoRead]:
    engine = get_engine()
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        rows = (session.query(CandidateVideo)
                .filter(CandidateVideo.job_id == job_id)
                .order_by(CandidateVideo.relevance_score.desc().nullslast(),
                          CandidateVideo.created_at.asc()).all())
        return [CandidateVideoRead(
            id=r.id, job_id=r.job_id, source_id=r.source_id,
            external_video_id=r.external_video_id, title=r.title, url=r.url,
            duration_seconds=r.duration_seconds, view_count=r.view_count,
            relevance_score=r.relevance_score, status=r.status,  # type: ignore[arg-type]
            published_at=r.published_at, created_at=r.created_at,
            creator=r.creator, description=r.description, licence=r.licence)
            for r in rows]
