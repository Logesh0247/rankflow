"""RANKFLOW AI — job management (Prompt 02, Phase 1).

Owns all persistent job state: creation, retrieval, history,
cancellation and per-stage checkpoints. Every method opens and
closes its own database session, so concurrent jobs never share
one. A checkpoint is a succeeded `agent_execution_logs` row whose
`detail` is JSON ``{"output": {...}, "output_references": {...}}``
— resuming a job re-reads those rows instead of re-running stages.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.errors import NotFoundError, ValidationError
from app.models.models import AgentExecutionLog, Job
from app.orchestrator.contracts import JobHistoryEntry
from app.orchestrator.states import AGENT_STAGES, STAGE_AGENT, STAGE_JOB_STATUS, TERMINAL_STAGES, WorkflowStage
from app.schemas.schemas import JobCreate, JobRead


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobService:
    def __init__(self, engine: Engine):
        self._sessions = sessionmaker(bind=engine)

    # ------------------------------------------------------------ create / read
    def create_job(self, payload: JobCreate) -> JobRead:
        with self._sessions() as session:
            job = Job(topic=payload.topic, requested_by=payload.requested_by,
                      status="created", current_stage=WorkflowStage.INITIALIZED.value)
            session.add(job)
            session.commit()
            session.refresh(job)
            return self._read(job)

    def get_job(self, job_id: str) -> JobRead:
        with self._sessions() as session:
            job = session.query(Job).filter(Job.id == job_id).first()
            if not job:
                raise NotFoundError(f"job not found: {job_id}")
            return self._read(job)

    def get_stage(self, job_id: str) -> WorkflowStage:
        """Current workflow stage, read fresh from the stored row."""
        with self._sessions() as session:
            job = self._require(session, job_id)
            return WorkflowStage(job.current_stage)

    def list_jobs(self, limit: int = 100) -> list[JobRead]:
        with self._sessions() as session:
            jobs = session.query(Job).order_by(Job.created_at.desc()).limit(limit).all()
            return [self._read(j) for j in jobs]

    def job_history(self, job_id: str) -> list[JobHistoryEntry]:
        """Job creation + every agent execution log, oldest first."""
        with self._sessions() as session:
            job = session.query(Job).filter(Job.id == job_id).first()
            if not job:
                raise NotFoundError(f"job not found: {job_id}")
            entries = [JobHistoryEntry(timestamp=job.created_at, event="job_created",
                                       stage=WorkflowStage.INITIALIZED.value,
                                       status=job.status, detail=f"topic: {job.topic}")]
            logs = (session.query(AgentExecutionLog)
                    .filter(AgentExecutionLog.job_id == job_id)
                    .order_by(AgentExecutionLog.created_at.asc()).all())
            for log in logs:
                entries.append(JobHistoryEntry(
                    timestamp=log.created_at,
                    event=f"agent_{log.status}",
                    stage=log.agent_name,
                    status=log.status,
                    detail=log.detail,
                ))
            return entries

    # ------------------------------------------------------------ state changes
    def set_stage(self, job_id: str, stage: WorkflowStage) -> JobRead:
        with self._sessions() as session:
            job = self._require(session, job_id)
            job.current_stage = stage.value
            job.status = STAGE_JOB_STATUS[stage]
            job.updated_at = _utcnow()
            session.commit()
            session.refresh(job)
            return self._read(job)

    def fail_job(self, job_id: str, error_message: str) -> JobRead:
        with self._sessions() as session:
            job = self._require(session, job_id)
            job.current_stage = WorkflowStage.FAILED.value
            job.status = "failed"
            job.error_message = error_message
            job.updated_at = _utcnow()
            session.commit()
            session.refresh(job)
            return self._read(job)

    def cancel_job(self, job_id: str) -> JobRead:
        """Cancel a job that is not already terminal. A job that
        already COMPLETED (awaiting_approval), FAILED or CANCELLED
        cannot be cancelled — that would rewrite history."""
        with self._sessions() as session:
            job = self._require(session, job_id)
            if job.status in ("awaiting_approval", "approved", "failed", "cancelled"):
                raise ValidationError(
                    f"job {job_id} is already {job.status} and cannot be cancelled"
                )
            job.current_stage = WorkflowStage.CANCELLED.value
            job.status = "cancelled"
            job.updated_at = _utcnow()
            session.commit()
            session.refresh(job)
            return self._read(job)

    def is_cancelled(self, job_id: str) -> bool:
        with self._sessions() as session:
            job = session.query(Job).filter(Job.id == job_id).first()
            return bool(job and job.status == "cancelled")

    # ------------------------------------------------------------ checkpoints / logs
    def record_log(self, job_id: str, agent_name: str, task_id: str | None,
                   status: str, detail: str | None = None,
                   duration_ms: int | None = None) -> None:
        with self._sessions() as session:
            session.add(AgentExecutionLog(
                job_id=job_id, agent_name=agent_name, task_id=task_id,
                status=status, detail=detail, duration_ms=duration_ms,
            ))
            session.commit()

    def checkpoint(self, job_id: str, stage: WorkflowStage) -> dict | None:
        """Persisted successful output for `stage`, or None.

        The checkpoint is the latest succeeded log row for the
        stage's agent whose detail parses as checkpoint JSON.
        """
        agent_id = _stage_agent(stage)
        with self._sessions() as session:
            logs = (session.query(AgentExecutionLog)
                    .filter(AgentExecutionLog.job_id == job_id,
                            AgentExecutionLog.agent_name == agent_id,
                            AgentExecutionLog.status == "succeeded")
                    .order_by(AgentExecutionLog.created_at.desc()).all())
            for log in logs:
                try:
                    data = json.loads(log.detail or "")
                except (ValueError, TypeError):
                    continue
                if isinstance(data, dict) and "output" in data:
                    return data
            return None

    def completed_stages(self, job_id: str) -> set[WorkflowStage]:
        with self._sessions() as session:
            logs = (session.query(AgentExecutionLog)
                    .filter(AgentExecutionLog.job_id == job_id,
                            AgentExecutionLog.status == "succeeded").all())
            done: set[WorkflowStage] = set()
            agent_to_stage = {STAGE_AGENT[s]: s for s in AGENT_STAGES}
            for log in logs:
                stage = agent_to_stage.get(log.agent_name)
                if stage is not None:
                    done.add(stage)
            return done

    def has_succeeded_task(self, task_id: str) -> bool:
        with self._sessions() as session:
            return session.query(AgentExecutionLog).filter(
                AgentExecutionLog.task_id == task_id,
                AgentExecutionLog.status == "succeeded").first() is not None

    # ------------------------------------------------------------ repair (Prompt 10)
    def repair_rounds_used(self, job_id: str) -> int:
        """Repair rounds already spent on this job. One marker row
        is written per round by `invalidate_checkpoints`, so the
        count survives re-renders (unlike QA's per-render counter,
        which resets when Production replaces the RenderedVideo
        row)."""
        with self._sessions() as session:
            return session.query(AgentExecutionLog).filter(
                AgentExecutionLog.job_id == job_id,
                AgentExecutionLog.agent_name == "orchestrator",
                AgentExecutionLog.status == "repair_round").count()

    def invalidate_checkpoints(self, job_id: str,
                               from_stage: WorkflowStage,
                               count_as_repair_round: bool = True) -> list[WorkflowStage]:
        """Drop the persisted successful checkpoints for
        `from_stage` and every later stage, so a resume re-runs
        them instead of reusing stale outputs. The QA report rows
        are kept (failure history), and one `repair_round` marker
        row is written so the round is counted even if the stages
        had no checkpoint to drop — unless `count_as_repair_round`
        is False (a user rejection is not a QA repair round)."""
        stages = [s for s in AGENT_STAGES if AGENT_STAGES.index(s) >= AGENT_STAGES.index(from_stage)]
        agent_ids = [STAGE_AGENT[s] for s in stages]
        with self._sessions() as session:
            if count_as_repair_round:
                session.add(AgentExecutionLog(
                    job_id=job_id, agent_name="orchestrator", task_id=None,
                    status="repair_round",
                    detail=json.dumps({
                        "from_stage": from_stage.value,
                        "invalidated_stages": [s.value for s in stages],
                    })))
            session.query(AgentExecutionLog).filter(
                AgentExecutionLog.job_id == job_id,
                AgentExecutionLog.agent_name.in_(agent_ids),
                AgentExecutionLog.status == "succeeded").delete(synchronize_session=False)
            session.commit()
        return stages

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _require(session, job_id: str) -> Job:
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise NotFoundError(f"job not found: {job_id}")
        return job

    @staticmethod
    def _read(job: Job) -> JobRead:
        return JobRead(id=job.id, topic=job.topic, status=job.status,  # type: ignore[arg-type]
                       current_stage=job.current_stage,
                       requested_by=job.requested_by, error_message=job.error_message,
                       created_at=job.created_at, updated_at=job.updated_at)

    # ------------------------------------------------------------ review (Prompt 11)
    def approve_job(self, job_id: str) -> JobRead:
        """Approve a completed video. Only a job in
        `awaiting_approval` can be approved — approving anything
        else would present an unfinished or failed video as final."""
        with self._sessions() as session:
            job = self._require(session, job_id)
            if job.status != "awaiting_approval":
                raise ValidationError(
                    f"job {job_id} is {job.status} and cannot be approved "
                    "(only a job awaiting approval can be approved)")
            job.status = "approved"
            job.updated_at = _utcnow()
            session.commit()
            session.refresh(job)
            return self._read(job)


def _stage_agent(stage: WorkflowStage) -> str:
    return STAGE_AGENT[stage]
