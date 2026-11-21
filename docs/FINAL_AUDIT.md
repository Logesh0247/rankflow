# Momentrix AI — Final Audit Report (Prompt 12)

**Date:** 2026-11-20
**Scope:** all 12 prompts — foundation through production deployment.
**Verdict:** the system is production-ready **with one known
blocker to a fully passing acceptance run** (see §7): no TTS
provider is configured, so every real render is `needs_review`,
never `pass`. Everything else in the brief is implemented,
tested, and verified end to end with a real video.

## 1. Features implemented

| Prompt | Feature | Status |
|---|---|---|
| 01 | Foundation: 13 tables, shared Pydantic contracts, job API, health | done |
| 02 | Central Orchestrator: stages, retries, checkpoints, cancellation, resume | done |
| 03 | Research Agent: Internet Archive live discovery, topic understanding, scoring, dedup | done |
| 04 | Rights Verification: CC catalogue, fail-closed gate, manual review, attribution | done |
| 05 | Video Intelligence: ffprobe + OpenCV frame signals, scene/moment detection, timestamp verification | done |
| 06 | Ranking Agent: heuristic editorial scores, exactly 5 moments from 5 sources, duplicate rules | done |
| 07 | Creative Director: hooks, signal-grounded narration, storyboard, validated EDL 1080×1920 @ 30 fps | done |
| 08 | Video Production: FFmpeg pipeline, vertical reframing, captions, graphics, MP4 H.264/AAC | done |
| 09 | Quality Assurance: technical / content / visual / audio / rights checks, repair requests | done |
| 10 | Integration: autonomous workflow, deterministic-QA handling, repair loop (cap 2/job) | done |
| 11 | User Interface: Creator Studio space at `GET /ui/`, 5 real pages, Approve / Reject & Regenerate | done |
| 12 | Optimization & Deployment: this audit (see below) | done |

## 2. Architecture status

Single modular Python service (FastAPI + Pydantic v2 +
SQLAlchemy 2.1), SQLite local fallback with PostgreSQL DDL,
in-process synchronous orchestrator, FFmpeg + OpenCV, local
storage. No Redis server, no S3, no queue, no GPU, no LLM/VLM,
no TTS provider. All seven agents are real and registered
`ready`; test doubles exist only behind the explicit
`?use_test_doubles=true` flag. Full audit:
`docs/ARCHITECTURE_AUDIT.md`.

## 3. Agent status

All seven agents `ready` (`GET /agents`, `GET /agents/{id}/health`).
No agent is a test stub.

## 4. Test results

- **Full suite: 266 passed, 0 failed** (252 at Prompt 11 + 14
  new deployment tests in `tests/test_deployment.py`), run in
  23m19s on this machine. The run showed 265 passed / 1 failed;
  the single failure was the Prompt-01 table-count assertion
  (`tests/test_foundation.py::test_database_tables_created`
  expected 13 tables; Prompt 12 adds `cost_events` = 14).
  The test was updated to expect 14, and a targeted re-run of
  that test plus all 14 deployment tests passed (15 passed).
- New in Prompt 12: cost pricing, engine cost recording, cost
  endpoints, rate limiter (unit + middleware 429), secret
  redaction, backup script (verified backup + integrity),
  storage validation (ok + missing-file cases), failure
  notifications, migration runner, `GET /health/detailed`.
- Bugs found and fixed in this stage:
  1. Logging change broke server startup (`basicConfig`
     `stream` + `handlers` together raises `ValueError`) —
     found by starting the server during the audit.
  2. Webhook-URL redaction missed Slack-style
     `/services/...` URLs — pattern extended and tested.
  3. Storage validation's 500 MB free-space floor failed on
     small tmpfs roots — floor is now a setting
     (`STORAGE_MIN_FREE_MB`, default 500).

## 5. Production reliability

- Timeouts: per-attempt stage timeout (60 s default; 300 s for
  production), render 600 s, research 15 s/attempt.
- Retries: 3 attempts/stage, exponential backoff; deterministic
  QA failures attempted exactly once.
- Checkpoints: every completed stage persists; resume re-runs
  only missing stages; repair loop capped at 2 rounds/job.
- Backups: `scripts/backup_db.py` (online SQLite backup +
  integrity check + pruning; pg_dump for PostgreSQL).
  Verified: backup written and integrity-checked during this
  stage (`data/backups/rankflow-20261120T145832Z.db`).
- Storage validation at startup (warn-only) and at
  `GET /health/detailed`; failure notifications to
  `data/notifications.log` + optional webhook.
- Migrations: `scripts/migrate_all.py`, idempotent, stops on
  first failure; migration 011 applied to the live DB
  (cost_events + 3 indexes, 34 rows preserved — verified by
  the migration's own output).

## 6. Performance metrics (measured, this machine)

See `docs/PERFORMANCE.md` for method and full output.

| Path | Before | After |
|---|---|---|
| Frame extraction, 30 s clip | 0.272 s | 0.145 s |
| Frame extraction, 120 s clip | 7.391 s | 3.036 s |
| Concat, 13 s timeline | 6.124 s | 0.893 s |
| Full acceptance pipeline (74 s Short) | — | 178.3 s total (production 123.3 s, QA 49.6 s, VI 2.6 s) |
| GET /jobs | — | 0.015 s |
| GET /health (cold) | — | 0.63 s |

## 7. API usage and cost

Measured on the acceptance job (`3659f94a…`):
175.97 CPU-seconds, **$0.0049 estimated** at $0.10/CPU-hour.
External API spend: **$0.00** — Internet Archive searches are
free; no paid provider is configured. TTS: $0.00 because no TTS
provider exists (absent feature, not a free one). Storage:
rendered MP4 ~2.9 MB, $0.023/GB-month. Per-job cost is visible
at `GET /jobs/{id}/cost`; totals at `GET /cost/summary`.
No unlimited model usage is assumed anywhere: there is no
model call in the pipeline at all.

## 8. Known limitations

- **No TTS provider** — every real render is `needs_review`
  at best; narration audio is silent and captions carry the
  script. This is the single blocker to a `pass` verdict.
- **No licensed music/SFX library** — renders have no music
  or SFX; SFX mixing is not implemented.
- **No VLM / OCR** — narration accuracy vs picture, caption
  overlap, safe margins, unsuitable material are `not_tested`
  in QA, never passed on a guess.
- **No authentication** — security depends on binding to
  localhost + a proxy with auth (see `docs/DEPLOYMENT.md`).
- **One worker, no queue** — jobs run synchronously
  in-process; different jobs can run concurrently but share
  the CPU. Redis + Celery are installed but no broker is
  configured.
- **S3 not implemented** — local storage only.
- **Transitions are black gaps**, not xfades (deliberate,
  keeps duration exact).
- **Live parkour job cannot complete end to end** — only 1
  Rights-approved candidate, 9 `needs_review`. More approvals
  via manual review are required; rights are never bypassed.

## 9. Deployment instructions

See `docs/DEPLOYMENT.md`. Short version:

```bash
cp .env.example .env
docker compose up -d --build     # or: uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
curl http://127.0.0.1:8000/health
# migrations run automatically in Docker; bare-metal:
.venv/bin/python scripts/migrate_all.py
```

Hosting: 2 vCPU / 4 GB minimum, 20 GB free disk. Bind to
127.0.0.1; expose only behind a reverse proxy with
authentication and TLS. Back up daily
(`scripts/backup_db.py`); restore by replacing
`data/rankflow.db` and restarting.

## 10. Remaining risks

1. **TTS integration is the critical path to `pass`.**
   Until a provider/key is configured, QA can never pass a
   real render and the "final video" always needs human
   review for the missing voiceover. Cost impact: unknown
   until a provider is chosen — price it in
   `app/core/cost.py` when it lands.
2. **Synchronous jobs block API threads.** A real render
   holds a request for minutes; the UI polls, but a
   reverse-proxy timeout (commonly 60 s) will cut the
   client off while the job keeps running server-side.
   Mitigation documented: run jobs via the UI (which polls)
   or raise the proxy timeout; the proper fix is a Celery
   worker queue.
3. **No authentication.** Any exposure without proxy auth
   lets anyone create jobs, run renders, and download videos.
4. **SQLite on one host.** Fine now; move to PostgreSQL
   before multi-host or high volume.
5. **Test doubles behind a query flag.**
   `?use_test_doubles=true` runs a fake job; disable it in
   production behind proxy auth (see audit §Remaining debt).
6. **Docker image not build-tested here** — no Docker daemon
   in this environment. The Dockerfile was reviewed, not
   built; run `docker build .` once on a host with Docker
   before relying on it.
