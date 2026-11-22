"""Background job runner (2026-11-21 Render health-check fix).

The problem: `POST /jobs/{id}/run` used to run the whole job
synchronously inside the HTTP request — a full job takes many
minutes, so the request stayed open that long. While it ran,
Render's health check (`GET /health`, 5 s timeout) had to share
the process with the job: a fresh DB engine per request, a cold
Neon connection waking suspended compute, and a long-lived
request on a 0.1-CPU free instance. When /health timed out,
Render restarted the service mid-job and the job was left
orphaned "running" — it could never finish, fail, or resume.

The fix: a run/resume starts the job in a daemon thread and
returns 202 immediately. /health (and every other read) then
answers from the request path with no job in the way, the UI
polls /jobs every 4 s as it already does, and a client
disconnect can no longer kill a job. A second run for a job
that is already running is refused with 409 instead of running
the same job twice. If the thread itself crashes (not a stage
failure — the engine already fails jobs honestly), the job is
marked failed with the cause so it never sits "running"
forever; a process death is covered by the startup cleanup in
main.py (`fail_interrupted_jobs`), and checkpoints survive so
the user can resume from the last completed stage.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from app.core.errors import AgentNotImplementedError
from app.core.logging import get_logger

logger = get_logger("orchestrator.runner")


class BackgroundJobRunner:
    """Runs at most one thread per job id. Threads are daemons:
    a graceful shutdown does not wait on a 10-minute render —
    the startup cleanup fails the interrupted job cleanly."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._running

    def start(self, job_id: str, target: Callable[[], None],
              fail_job: Callable[[str], None]) -> bool:
        """Start `target` in a background thread for `job_id`.

        Returns False when that job is already running (the
        caller should answer 409). `fail_job` is called with
        an error message if the thread raises unexpectedly, so
        the job is failed cleanly instead of stuck "running".
        """
        with self._lock:
            if job_id in self._running:
                return False
            self._running.add(job_id)

        def _run() -> None:
            try:
                target()
            except AgentNotImplementedError:
                # The agent simply does not exist yet: the engine
                # leaves the job as it was so it can run once the
                # agent is built — do not fail it for that.
                logger.warning("job %s not run: agent not implemented", job_id)
            except Exception as exc:  # noqa: BLE001 — a crashed thread must not orphan the job
                logger.exception("job %s background run crashed: %s", job_id, exc)
                try:
                    fail_job(f"background run crashed: {exc}")
                except Exception:  # noqa: BLE001 — failing the job must never raise
                    logger.exception("job %s could not be marked failed after a crash",
                                     job_id)
            finally:
                with self._lock:
                    self._running.discard(job_id)

        thread = threading.Thread(target=_run, daemon=True,
                                  name=f"momentrix-job-{job_id[:8]}")
        thread.start()
        return True


# One process-wide runner: the app runs a single uvicorn worker
# (a second worker would run the same job twice — see Dockerfile),
# so one in-process registry is the whole truth.
RUNNER = BackgroundJobRunner()
