"""Job endpoints (Prompt 01 scope only).

POST /jobs creates a job and stores it. GET /jobs lists jobs.
GET /jobs/{id} reads one job. No agent runs in Prompt 01 — a created
job stays in status 'created' until the orchestrator (Prompt 02)
picks it up. That is intentional, not a bug.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.database.session import get_session, init_db, make_engine
from app.models.models import Job
from app.schemas.schemas import JobCreate, JobRead

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _session() -> Session:
    return get_session(make_engine())


@router.post("", response_model=JobRead, status_code=201)
def create_job(payload: JobCreate, session: Session = Depends(_session)) -> JobRead:
    init_db(make_engine())
    job = Job(topic=payload.topic, requested_by=payload.requested_by, status="created")
    session.add(job)
    session.commit()
    session.refresh(job)
    return JobRead(id=job.id, topic=job.topic, status=job.status, current_stage=job.current_stage,
                   requested_by=job.requested_by, error_message=job.error_message,
                   created_at=job.created_at, updated_at=job.updated_at)


@router.get("", response_model=list[JobRead])
def list_jobs(session: Session = Depends(_session)) -> list[JobRead]:
    init_db(make_engine())
    jobs = session.query(Job).order_by(Job.created_at.desc()).limit(100).all()
    return [JobRead(id=j.id, topic=j.topic, status=j.status, current_stage=j.current_stage,
                    requested_by=j.requested_by, error_message=j.error_message,
                    created_at=j.created_at, updated_at=j.updated_at) for j in jobs]


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: str, session: Session = Depends(_session)) -> JobRead:
    job = session.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return JobRead(id=job.id, topic=job.topic, status=job.status, current_stage=job.current_stage,
                   requested_by=job.requested_by, error_message=job.error_message,
                   created_at=job.created_at, updated_at=job.updated_at)
