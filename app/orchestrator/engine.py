"""Momentrix AI — workflow engine (Prompt 02, Phases 3-5).

Runs jobs through the fixed stage sequence, one stage at a time,
in-process and synchronously (no Redis/Celery broker exists in this
environment — see docs/ARCHITECTURE.md). Each stage:

1. is skipped if a persisted successful checkpoint already exists
   (duplicate-task prevention + job resumption);
2. is refused if its dependencies are not satisfied;
3. runs the registered executor under a per-attempt timeout;
4. has its output validated against the stage's Pydantic schema —
   invalid output is a failure, never accepted;
5. is retried with exponential backoff up to `max_attempts` —
   except a DeterministicError (Prompt 10), which is never
   retried: the same input would fail the same way, so the job
   fails once and recovery happens via `resume_job`, which
   applies QA repair requests or re-runs from a stage the human
   names after supplying new evidence;
6. on final failure marks the job FAILED with the error stored on
   the job and in the execution log — failures are never discarded
   silently, and a failure in one stage never touches other jobs.

Cancellation is honoured between stages.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.engine import Engine

from app.core.config import Settings, get_settings
from app.core.cost import CostService
from app.core.errors import (
    AgentNotImplementedError,
    DeterministicError,
    ValidationError,
)
from app.core.logging import get_logger
from app.orchestrator.contracts import (
    STAGE_OUTPUT_SCHEMAS,
    OrchestratorResponse,
    OrchestratorTask,
    ValidationResult,
)
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import AgentRegistry, build_default_registry
from app.orchestrator.states import (
    AGENT_STAGES,
    STAGE_AGENT,
    WorkflowStage,
    dependencies_satisfied,
    next_stage,
)
from app.schemas.schemas import JobRead

logger = get_logger("orchestrator")


class WorkflowEngine:
    def __init__(self, engine: Engine, settings: Settings | None = None,
                 registry: AgentRegistry | None = None):
        self._engine = engine
        self.settings = settings or get_settings()
        self.registry = registry or build_default_registry()
        self.jobs = JobService(engine)

    # ------------------------------------------------------------ public API
    def run_job(self, job_id: str) -> JobRead:
        """Run a job from its current stage to COMPLETED.

        A job that already reached a terminal stage is returned
        unchanged — running it again never re-executes stages
        (duplicate prevention). A FAILED job is resumed with
        `resume_job`, not `run_job`.
        """
        job = self.jobs.get_job(job_id)
        if job.status == "failed":
            raise ValidationError(
                f"job {job_id} failed; resume it with resume_job() instead of re-running it")
        if job.status in ("awaiting_approval", "approved", "cancelled"):
            return job
        return self._run_from(job_id)

    def resume_job(self, job_id: str, *, apply_repairs: bool = True,
                   rerun_from_stage: WorkflowStage | str | None = None) -> JobRead:
        """Resume a FAILED (or interrupted) job from the first stage
        without a persisted successful checkpoint.

        Prompt 10 repair handling, applied before the run:
        * `rerun_from_stage` — after human intervention (e.g. more
          candidates approved after an insufficient-candidates
          failure at RANKING), invalidate that stage and every
          later stage so the resume re-runs them from the new
          evidence instead of reusing stale checkpoints.
        * otherwise, if the latest QA report is a `fail` with
          repair requests targeting an agent stage, invalidate
          from the earliest such stage (capped at
          `qa_max_repair_attempts` repair rounds per job) so the
          resume re-renders / re-plans and QA re-inspects the
          corrected output. Requests that target only
          `manual_review` / `config` (e.g. "configure a TTS
          provider") cannot be fixed by re-running anything, so
          resume refuses with the manual action named instead of
          failing the same way again.
        """
        job = self.jobs.get_job(job_id)
        if job.status in ("awaiting_approval", "approved", "cancelled"):
            raise ValidationError(f"job {job_id} is {job.status} and cannot be resumed")
        if rerun_from_stage is not None:
            stage = (rerun_from_stage if isinstance(rerun_from_stage, WorkflowStage)
                     else WorkflowStage(rerun_from_stage))
            if stage not in AGENT_STAGES:
                raise ValidationError(f"cannot re-run from non-agent stage {stage.value}")
            self.jobs.invalidate_checkpoints(job_id, stage)
            logger.info("job %s resume re-runs from %s after human intervention",
                        job_id, stage.value)
        elif apply_repairs:
            self._apply_qa_repairs(job_id)
        return self._run_from(job_id)

    def regenerate_job(self, job_id: str) -> JobRead:
        """Reject-and-regenerate (Prompt 11): the user rejected the
        completed video, so re-render it from the approved EDL and
        QA it again. Only an `awaiting_approval` job can be
        regenerated — the earlier stages (research through creative
        planning) keep their checkpoints, so only VIDEO_PRODUCTION
        and QUALITY_ASSURANCE re-run. A user rejection is not a QA
        repair round, so it does not consume the repair cap."""
        job = self.jobs.get_job(job_id)
        if job.status != "awaiting_approval":
            raise ValidationError(
                f"job {job_id} is {job.status} and cannot be regenerated "
                "(only a job awaiting approval can be rejected and regenerated)")
        self.jobs.invalidate_checkpoints(
            job_id, WorkflowStage.VIDEO_PRODUCTION, count_as_repair_round=False)
        self.jobs.set_stage(job_id, WorkflowStage.VIDEO_PRODUCTION)
        return self._run_from(job_id)

    def _apply_qa_repairs(self, job_id: str) -> None:
        """Apply the latest failed QA report's repair requests, if
        any target an agent stage. No-op when there is no QA report
        or the latest verdict is not a failure."""
        from app.agents.quality_assurance.service import QAService

        try:
            report = QAService(self._engine, self.settings).get_report(job_id)
        except Exception:
            return  # no QA report yet — plain resume
        if report.verdict != "fail":
            return
        requests = list(report.repair_requests or [])
        actionable = []
        for req in requests:
            try:
                stage = WorkflowStage(str(req.get("target_stage") or ""))
            except ValueError:
                continue  # "manual_review" and anything unmapped
            if stage in AGENT_STAGES:
                actionable.append(stage)
        if not actionable:
            details = "; ".join(report.issues) or "no repair requests recorded"
            raise ValidationError(
                f"job {job_id} failed QA with no automated repair — "
                f"human intervention is required before resuming: {details}")
        rounds_used = self.jobs.repair_rounds_used(job_id)
        max_rounds = max(1, int(self.settings.qa_max_repair_attempts))
        if rounds_used >= max_rounds:
            raise ValidationError(
                f"job {job_id} has used {rounds_used}/{max_rounds} QA repair "
                "rounds — repair attempts exhausted; fix the underlying "
                "cause (or supply new evidence) and start a fresh job")
        earliest = min(actionable, key=lambda s: AGENT_STAGES.index(s))
        invalidated = self.jobs.invalidate_checkpoints(job_id, earliest)
        logger.info("job %s QA repair round %d/%d: re-running %s",
                    job_id, rounds_used + 1, max_rounds,
                    ", ".join(s.value for s in invalidated))

    # ------------------------------------------------------------ core loop
    def _run_from(self, job_id: str) -> JobRead:
        current = self.jobs.get_stage(job_id)
        completed = self.jobs.completed_stages(job_id)
        context: dict[str, Any] = self._rebuild_context(job_id, completed)

        if current == WorkflowStage.INITIALIZED:
            stage: WorkflowStage | None = AGENT_STAGES[0]
        elif current in AGENT_STAGES:
            stage = current if current not in completed else next_stage(current)
        elif current == WorkflowStage.FAILED:
            # Resume from the first stage without a checkpoint.
            pending = [s for s in AGENT_STAGES if s not in completed]
            stage = pending[0] if pending else WorkflowStage.COMPLETED
        elif current == WorkflowStage.COMPLETED:
            stage = WorkflowStage.COMPLETED
        else:  # CANCELLED or anything else terminal — nothing to run.
            return job

        if stage == WorkflowStage.COMPLETED:
            self.jobs.set_stage(job_id, WorkflowStage.COMPLETED)
            return self.jobs.get_job(job_id)

        while stage is not None:
            if stage == WorkflowStage.COMPLETED:
                break
            if self.jobs.is_cancelled(job_id):
                self.jobs.set_stage(job_id, WorkflowStage.CANCELLED)
                logger.info("job %s cancelled before stage %s", job_id, stage.value)
                return self.jobs.get_job(job_id)
            if not dependencies_satisfied(stage, completed):
                return self._fail(job_id, stage,
                                  f"dependencies not satisfied for stage {stage.value}")
            if stage in completed:
                # Checkpoint exists: reuse it, do not re-execute.
                stage = next_stage(stage)
                continue
            try:
                output = self._execute_stage(job_id, stage, context)
            except AgentNotImplementedError:
                # Not a job failure: the agent simply does not exist
                # yet. Surface it (HTTP 501) and leave the job as it
                # was, so it can run once the agent is built.
                raise
            except Exception as exc:  # failure isolation: one stage, one job
                return self._fail(job_id, stage, str(exc))
            context[stage.value] = output
            completed.add(stage)
            self.jobs.set_stage(job_id, stage)
            stage = next_stage(stage)

        self.jobs.set_stage(job_id, WorkflowStage.COMPLETED)
        logger.info("job %s completed", job_id)
        return self.jobs.get_job(job_id)

    def _fail(self, job_id: str, stage: WorkflowStage, error: str) -> JobRead:
        # Prompt 12, Phase 4: a job failure is never silent — log it,
        # append it to the notifications log, and post the webhook
        # when one is configured. Notification delivery never raises.
        from app.core.notifications import notify_job_failed
        notify_job_failed(job_id, stage.value, error, self.settings)
        return self.jobs.fail_job(job_id, f"{stage.value}: {error}")

    def _rebuild_context(self, job_id: str, completed: set[WorkflowStage]) -> dict[str, Any]:
        context: dict[str, Any] = {}
        for stage in AGENT_STAGES:
            if stage in completed:
                checkpoint = self.jobs.checkpoint(job_id, stage)
                if checkpoint:
                    context[stage.value] = checkpoint.get("output", {})
        return context

    # ------------------------------------------------------------ one stage
    def _execute_stage(self, job_id: str, stage: WorkflowStage,
                       context: dict[str, Any]) -> dict[str, Any]:
        agent_id = STAGE_AGENT[stage]
        executor = self.registry.executor_for(agent_id)  # raises if not implemented
        schema = STAGE_OUTPUT_SCHEMAS[stage.value]
        job = self.jobs.get_job(job_id)
        max_attempts = max(1, self.settings.orchestrator_max_attempts)

        for attempt in range(1, max_attempts + 1):
            # task_id is stored in agent_execution_logs.task_id
            # (VARCHAR(64)): 36 (UUID) + 1 + longest stage name
            # (RIGHTS_VERIFICATION, 19) + 1 + 1 attempt digit = 58
            # max. Do not lengthen this format without widening
            # that column (see migration 012 — Postgres enforces
            # VARCHAR lengths, SQLite does not).
            task_id = f"{job_id}:{stage.value}:{attempt}"
            if self.jobs.has_succeeded_task(task_id):
                # Duplicate task: already succeeded, do not run again.
                checkpoint = self.jobs.checkpoint(job_id, stage)
                if checkpoint:
                    return checkpoint.get("output", {})
            task = OrchestratorTask(
                task_id=task_id,
                job_id=job_id,
                agent_id=agent_id,
                input_references=self._input_references(stage, context),
                expected_output_schema=schema.__name__,
                deadline=datetime.now(timezone.utc) + timedelta(
                    seconds=self.settings.orchestrator_timeout_seconds),
                retry_count=attempt - 1,
                attempt=attempt,
                input={"topic": job.topic, "prior_stage_outputs": context},
            )
            started = time.monotonic()
            self.jobs.record_log(job_id, agent_id, task_id, "started",
                                 detail=json.dumps({"stage": stage.value, "attempt": attempt}))
            try:
                raw = self._call_with_timeout(executor, task)
                output = self._validate_output(schema, raw)
            except DeterministicError as exc:
                # Prompt 10: re-running the same stage on the same
                # input cannot change the result — one attempt only,
                # no backoff, no retry. Recovery is via resume
                # (which re-runs the owning stage) or human action.
                duration_ms = int((time.monotonic() - started) * 1000)
                self.jobs.record_log(job_id, agent_id, task_id, "failed",
                                     detail=json.dumps({"stage": stage.value, "attempt": attempt,
                                                        "error": str(exc),
                                                        "deterministic": True}),
                                     duration_ms=duration_ms)
                logger.warning("job %s stage %s deterministic failure (no retry): %s",
                               job_id, stage.value, exc)
                raise
            except Exception as exc:
                duration_ms = int((time.monotonic() - started) * 1000)
                self.jobs.record_log(job_id, agent_id, task_id, "failed",
                                     detail=json.dumps({"stage": stage.value, "attempt": attempt,
                                                        "error": str(exc)}),
                                     duration_ms=duration_ms)
                logger.warning("job %s stage %s attempt %d failed: %s",
                               job_id, stage.value, attempt, exc)
                if attempt < max_attempts:
                    time.sleep(self.settings.orchestrator_backoff_base_seconds * (2 ** (attempt - 1)))
                    continue
                raise RuntimeError(
                    f"stage {stage.value} failed after {max_attempts} attempt(s): {exc}"
                ) from exc
            duration_ms = int((time.monotonic() - started) * 1000)
            response = OrchestratorResponse(
                task_id=task_id, job_id=job_id, agent_id=agent_id, status="succeeded",
                output_references=self._output_references(stage, output),
                validation=ValidationResult(valid=True),
                execution_metadata={"duration_ms": duration_ms, "attempt": attempt,
                                    "executor": self.registry.get(agent_id).execution_method},
            )
            self.jobs.record_log(job_id, agent_id, task_id, "succeeded",
                                 detail=json.dumps({"output": output,
                                                    "output_references": response.output_references}),
                                 duration_ms=duration_ms)
            # Prompt 12, Phase 3: price the stage's measured compute.
            # Guarded — cost recording must never break a job.
            try:
                CostService(self._engine, self.settings).record_compute(
                    job_id, stage.value, duration_ms / 1000.0)
            except Exception:  # noqa: BLE001 — best-effort only
                logger.debug("cost event skipped for job %s stage %s",
                             job_id, stage.value)
            return output
        raise RuntimeError(f"stage {stage.value} exhausted retries without a result")

    # ------------------------------------------------------------ helpers
    def _call_with_timeout(self, executor, task: OrchestratorTask) -> Any:
        """Run one executor call; a slow agent is a timeout failure,
        not a hang."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(executor, task.model_dump(mode="json"))
            try:
                return future.result(timeout=self.settings.orchestrator_timeout_seconds)
            except FutureTimeoutError:
                future.cancel()
                raise TimeoutError(
                    f"agent {task.agent_id} exceeded "
                    f"{self.settings.orchestrator_timeout_seconds}s timeout"
                ) from None

    @staticmethod
    def _validate_output(schema, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError(
                f"agent output must be a JSON object, got {type(raw).__name__}")
        try:
            validated = schema.model_validate(raw)
        except PydanticValidationError as exc:
            raise ValidationError(f"agent output failed {schema.__name__} validation: {exc}") from exc
        return validated.model_dump(mode="json")

    @staticmethod
    def _input_references(stage: WorkflowStage, context: dict[str, Any]) -> dict[str, str | list[str]]:
        """What the stage may read, from earlier stages only."""
        refs: dict[str, str | list[str]] = {}
        if stage == WorkflowStage.RIGHTS_VERIFICATION and "RESEARCH" in context:
            refs["candidate_ids"] = context["RESEARCH"].get("candidate_ids", [])
        if stage == WorkflowStage.VIDEO_ANALYSIS and "RIGHTS_VERIFICATION" in context:
            refs["approved_candidate_ids"] = context["RIGHTS_VERIFICATION"].get("approved_candidate_ids", [])
        if stage == WorkflowStage.RANKING and "VIDEO_ANALYSIS" in context:
            refs["moment_ids"] = context["VIDEO_ANALYSIS"].get("moment_ids", [])
        if stage == WorkflowStage.CREATIVE_PLANNING and "RANKING" in context:
            refs["ranked_moment_ids"] = [r["moment_id"] for r in context["RANKING"].get("ranks", [])]
        if stage == WorkflowStage.VIDEO_PRODUCTION and "CREATIVE_PLANNING" in context:
            refs["storyboard_id"] = context["CREATIVE_PLANNING"].get("storyboard_id", "")
        if stage == WorkflowStage.QUALITY_ASSURANCE and "VIDEO_PRODUCTION" in context:
            refs["rendered_video_id"] = context["VIDEO_PRODUCTION"].get("rendered_video_id", "")
        return refs

    @staticmethod
    def _output_references(stage: WorkflowStage, output: dict[str, Any]) -> dict[str, str | list[str]]:
        keys = {
            WorkflowStage.RESEARCH: ["candidate_ids"],
            WorkflowStage.RIGHTS_VERIFICATION: ["approved_candidate_ids"],
            WorkflowStage.VIDEO_ANALYSIS: ["moment_ids"],
            WorkflowStage.RANKING: [],
            WorkflowStage.CREATIVE_PLANNING: ["storyboard_id"],
            WorkflowStage.VIDEO_PRODUCTION: ["rendered_video_id", "file_path"],
            WorkflowStage.QUALITY_ASSURANCE: [],
        }[stage]
        refs = {k: output[k] for k in keys if k in output}
        if stage == WorkflowStage.RANKING:
            refs["ranked_moment_ids"] = [r["moment_id"] for r in output.get("ranks", [])]
        if stage == WorkflowStage.QUALITY_ASSURANCE:
            refs["verdict"] = output.get("verdict", "")
        return refs
