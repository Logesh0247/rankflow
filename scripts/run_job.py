"""Demo: run a complete simulated job through the orchestrator.

Uses TEST-ONLY test doubles (Prompt 02, Phase 6) — a fast demo of
the orchestrator mechanics, not video production. The seven real
agents exist now (Prompts 03-09, integrated in Prompt 10); to run
a real job, create one and POST /jobs/{id}/run, or use
scripts/run_production.py / scripts/run_qa.py per stage. Two
demonstrations:

1. A clean run: topic -> all 8 workflow states -> awaiting_approval.
2. Failure recovery: the ranking double fails twice, the job is
   marked FAILED, then resume completes it from the failed stage
   without re-running the earlier stages.

Usage:
    .venv/bin/python scripts/run_job.py ["topic"]
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from app.core.config import Settings
from app.database.session import init_db, make_engine
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.orchestrator.test_doubles import DoubleConfig, build_test_registry
from app.schemas.schemas import JobCreate


def _settings() -> Settings:
    # Fast demo: short backoff, generous timeout.
    return Settings(orchestrator_backoff_base_seconds=0.05, orchestrator_timeout_seconds=10.0)


def _stages(engine, job_id: str) -> list[str]:
    service = JobService(engine)
    return [e.stage or "" for e in service.job_history(job_id) if e.event.startswith("agent_succeeded")]


def main() -> None:
    topic = sys.argv[1] if len(sys.argv) > 1 else "TOP 5 CRAZIEST PARKOUR FAILS"
    engine = make_engine()
    init_db(engine)

    print("== 1. Clean simulated run ==")
    service = JobService(engine)
    job = service.create_job(JobCreate(topic=topic))
    print(f"job {job.id} topic={job.topic!r} status={job.status}")
    runner = WorkflowEngine(engine, _settings(), build_test_registry())
    done = runner.run_job(job.id)
    print(f"final status={done.status} (workflow COMPLETED -> awaiting_approval)")
    print("succeeded stages:", " -> ".join(_stages(engine, job.id)))

    print("\n== 2. Failure recovery ==")
    job2 = service.create_job(JobCreate(topic=topic + " (recovery demo)"))
    failing = build_test_registry({"ranking": DoubleConfig(fail_attempts={1, 2, 3})})
    runner2 = WorkflowEngine(engine, _settings(), failing)
    failed = runner2.run_job(job2.id)
    print(f"after always-failing ranking: status={failed.status} error={failed.error_message!r}")
    print("succeeded stages before failure:", " -> ".join(_stages(engine, job2.id)))
    resumed = WorkflowEngine(engine, _settings(), build_test_registry()).resume_job(job2.id)
    print(f"after resume with working doubles: status={resumed.status}")
    print("succeeded stages after resume:", " -> ".join(_stages(engine, job2.id)))
    print("\nDone. This was the orchestrator + test doubles only — "
          "the seven real agents are built and integrated (see "
          "docs/INTEGRATION.md); run a real job with POST /jobs/{id}/run.")


if __name__ == "__main__":
    main()
