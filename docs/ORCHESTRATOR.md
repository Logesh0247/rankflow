# Momentrix AI — Central Orchestrator (Prompt 02)

The orchestrator is the only component that runs jobs. It accepts a
topic, creates a job, hands one validated task at a time to the
registered agent for that stage, validates the response, persists a
checkpoint, and moves to the next stage. It does not do any research,
rights checking, video analysis, ranking, creative work, production
or QA itself — those are the seven agents (Prompts 03–09), which do
not exist yet.

## Workflow

```
INITIALIZED → RESEARCH → RIGHTS_VERIFICATION → VIDEO_ANALYSIS
→ RANKING → CREATIVE_PLANNING → VIDEO_PRODUCTION
→ QUALITY_ASSURANCE → COMPLETED
```

`COMPLETED` maps to job status `awaiting_approval` (the Short is
ready for the user). `FAILED` and `CANCELLED` are terminal. A stage
never runs before its dependencies are satisfied, and a stage with
a persisted successful checkpoint is never re-executed.

## Components

| File | Role |
|---|---|
| `app/orchestrator/states.py` | Stage enum, order, stage→agent and stage→job-status maps, dependency check |
| `app/orchestrator/contracts.py` | `OrchestratorTask` / `OrchestratorResponse`, `AgentDescriptor`, `AgentHealth`, per-stage output schemas (`ResearchOutput` … `QAOutput`) |
| `app/orchestrator/registry.py` | The seven agents: id, description, input/output schema, execution method, status, health. Real agents have no executor (`not_implemented`) |
| `app/orchestrator/jobs.py` | `JobService`: create / get / list / history / cancel, stage changes, checkpoints (succeeded `agent_execution_logs` rows), duplicate-task check |
| `app/orchestrator/engine.py` | `WorkflowEngine`: `run_job`, `resume_job`, per-stage timeout + retry with exponential backoff, output validation, failure isolation, cancellation between stages |
| `app/orchestrator/test_doubles.py` | **TEST-ONLY** deterministic doubles for orchestrator testing |
| `app/api/orchestrator.py` | `GET /agents`, `GET /agents/{id}/health`, `POST /jobs/{id}/run`, `POST /jobs/{id}/resume`, `POST /jobs/{id}/cancel`, `GET /jobs/{id}/history` |
| `scripts/run_job.py` | Demo: clean simulated run + failure-recovery run (test doubles) |
| `scripts/migrate_002_add_current_stage.py` | Adds `jobs.current_stage` to Prompt 01 databases (idempotent) |
| `tests/test_orchestrator.py` | 19 orchestrator tests |

## Task / response contracts (Phase 4)

Every task contains: job id, task id, agent id, input references,
expected output schema, deadline, retry count (= attempt − 1).
Every response contains: status, output references, validation
results, errors, execution metadata (duration, attempt, executor).

## Reliability (Phase 5)

- Per-attempt timeout (`orchestrator_timeout_seconds`, default 60s),
  enforced with a thread pool — a slow agent fails the attempt, it
  does not hang the job.
- Retries: `orchestrator_max_attempts` (default 3) per stage, with
  exponential backoff `base * 2 ** (attempt − 1)`
  (`orchestrator_backoff_base_seconds`, default 1s). All three are
  env-configurable (`ORCHESTRATOR_MAX_ATTEMPTS`,
  `ORCHESTRATOR_TIMEOUT_SECONDS`, `ORCHESTRATOR_BACKOFF_BASE_SECONDS`).
- Failure isolation: an exception in one stage fails only that job;
  the error is stored on the job (`error_message`) and in the
  execution log — never silently discarded.
- Persistent checkpoints: after each stage succeeds, its output is
  stored in `agent_execution_logs.detail` as JSON. `resume_job`
  rebuilds the job context from those rows and re-runs only the
  stages without a checkpoint.
- Duplicate-task prevention: task ids are deterministic
  (`{job_id}:{stage}:{attempt}`); a succeeded task id is never
  re-executed, and re-running a completed job returns it unchanged.
- Cancellation is honoured between stages; a terminal job
  (awaiting_approval / approved / failed / cancelled) cannot be
  cancelled or resumed.

## What is NOT built

- No Redis/Celery broker: runs are synchronous and in-process.
  No Postgres/Redis server exists in this environment (see
  `docs/ARCHITECTURE.md`); SQLite remains the local fallback.
- The migration in `scripts/migrate_002_add_current_stage.py` must
  be run once on any database created by Prompt 01, because
  `create_all()` does not add columns to existing tables.
- The seven real agents are all built (Prompts 03-09) and
  integrated (Prompt 10, see `docs/INTEGRATION.md`). The old
  "501 for a missing agent" behaviour now only applies when a
  registry is built without an engine (tests) — a plain run on a
  real database goes end to end.

## Running it

```bash
cd ~/workspace/rankflow_ai
.venv/bin/python scripts/migrate_002_add_current_stage.py  # Prompt 01 DBs only
.venv/bin/python scripts/run_job.py "TOP 5 CRAZIEST PARKOUR FAILS"
.venv/bin/python -m pytest tests/ -v
```

## Tests (19, all passing with the 19 Prompt 01 tests — 38 total at
the time; the suite has since grown — see `docs/INTEGRATION.md`
for the current count and the end-to-end integration tests)

Registry (7 agents, no executors, health), job create/get/history/
cancel, stage order + dependency rules, contract validation
(including the five-unique-sources ranking rule), a full simulated
job through all 8 workflow states, idempotent re-run, 501 for a
missing agent, data handoffs (ranking receives video-analysis
moments, creative receives ranking, QA receives production),
retry after one failure, invalid-output rejection + retry, timeout
→ FAILED with the error stored, failure recovery resuming from the
failed stage without re-running completed stages, resume of a
completed job rejected, concurrent-job isolation, and the API
endpoints including the test-doubles run.
