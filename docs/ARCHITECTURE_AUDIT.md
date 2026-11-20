# RANKFLOW AI — Architecture Audit (Prompt 12, Phase 1)

Audited 2026-11-20 against the Prompt 11 codebase (252 tests
passing). Every claim below was checked in the code or measured;
nothing is asserted from the brief alone.

## System shape

Single modular Python service: FastAPI + Pydantic v2 + SQLAlchemy
2.1, SQLite local fallback (PostgreSQL DDL in
`app/database/schema.sql`), in-process synchronous orchestrator,
FFmpeg + OpenCV for video, local-filesystem storage. No Redis
server, no S3, no queue, no GPU, no LLM/VLM, no TTS provider.
The UI is served by the same backend at `GET /ui/`.

## Agent status (all seven real)

| Agent | Status | Evidence |
|---|---|---|
| Research | ready | Internet Archive live, no key; YouTube implemented, disabled without key; test provider test-only |
| Rights | ready | CC catalogue v1–4 + CC0 + PDM; fail-closed gate; manual review cannot override a restrictive licence |
| Video Intelligence | ready | ffprobe + OpenCV frame signals only; no VLM; timestamps re-verified |
| Ranking | ready | heuristic editorial scores; exactly 5 from 5 sources; duplicate rules |
| Creative Director | ready | deterministic templates; signal-grounded narration; forbidden-word guard |
| Production | ready | real FFmpeg pipeline; stream-copy concat (Prompt 12); no TTS / no licensed audio |
| Quality Assurance | ready | ffprobe + OpenCV + volumedetect; pass / needs_review / fail; repair requests |

No agent is a test stub. Test doubles exist only in
`app/orchestrator/test_doubles.py` and are reachable only via the
explicit `?use_test_doubles=true` query parameter; the default
registry contains no doubles (verified by
`tests/test_integration.py`).

## Findings

### Fixed in this stage
1. **Logging broke startup (real bug, found by this audit).**
   `configure_logging` passed both `stream=` and `handlers=` to
   `logging.basicConfig`, which raises `ValueError` — the server
   would not start. Fixed by passing only `handlers=`.
   The redacting filter itself was verified by
   `tests/test_deployment.py::test_log_redaction_masks_keys`.
2. **Concat re-encoded the video twice.** `concat_segments`
   re-encoded already-uniform 1080×1920 segments. Now the video
   stream is copied and only audio is re-encoded, with a full
   re-encode fallback. Measured: 6.12 s → 0.89 s on a 13 s
   timeline (6.9×), output still validates (see
   `docs/PERFORMANCE.md`).
3. **Frame extraction decoded every frame.** `extract_samples`
   read the whole video to sample 1 fps. Now seek-based, with the
   sequential scan as fallback. Measured: 120 s clip 7.39 s →
   3.04 s (2.4×), identical samples and scene boundaries.
4. **Missing indexes.** `storyboards`, `narrations` and
   `edit_instructions` had no `job_id` index. Added (migration
   011); `EXPLAIN QUERY PLAN` now shows index search on every
   hot lookup.
5. **No cost visibility.** Added `cost_events` + `CostService` +
   `GET /jobs/{id}/cost` and `GET /cost/summary` (see
   `docs/DEPLOYMENT.md` for rates).
6. **No failure notification path.** Added
   `app/core/notifications.py` (log + optional webhook) hooked
   into the engine's single failure funnel.
7. **No storage validation.** Added `app/storage/validate.py`,
   run at startup (warn-only) and at `GET /health/detailed`.
8. **No rate limiting.** Added per-IP token-bucket middleware
   (600/min default, `/health` exempt).
9. **No backup tooling.** Added `scripts/backup_db.py`
   (online backup + integrity check + pruning) and
   `scripts/migrate_all.py` (ordered, idempotent, stops on
   first failure).

### Remaining technical debt (accepted, not fixed here)
- **Synchronous in-process jobs.** `POST /jobs/{id}/run` blocks
  until the whole pipeline finishes (minutes for a real render).
  The UI polls; a slow client holds a worker thread. A real
  queue (Celery + Redis) is the correct fix and is a bigger
  change than this stage — see Remaining risks in
  `docs/FINAL_AUDIT.md`.
- **No authentication.** The API has no login; security depends
  on binding to localhost + a proxy with auth. Documented in
  `docs/DEPLOYMENT.md`.
- **In-memory rate limiting.** Correct for one worker; must move
  to Redis for multi-worker.
- **SFX mixing not implemented.** Production detects SFX files
  but does not mix them (documented in the manifest).
- **Transitions are black gaps, not xfades.** Deliberate
  (keeps the total duration exact); documented in the manifest.
- **Stale docs fixed in Prompt 10;** `docs/ORCHESTRATOR.md`
  was rewritten then. No stale "seven agents not built" text
  remains (checked by grep).
- **S3 backend not implemented.** `storage_backend: s3` raises
  `StorageError`. Local storage only.
- **Test doubles reachable via query parameter.** Intended for
  testing, but `?use_test_doubles=true` on a production host
  would run a fake job. Mitigation: never expose the API
  without proxy auth; a future hardening step is to disable
  the flag unless `APP_ENV != production`.

## Performance (measured, this machine)

| Path | Before | After | How measured |
|---|---|---|---|
| Frame extraction, 30 s clip | 0.27 s | 0.14 s | `scripts/benchmark.py` |
| Frame extraction, 120 s clip | 7.39 s | 3.04 s | `scripts/benchmark.py` |
| Concat, 13 s timeline | 6.12 s | 0.89 s | `scripts/benchmark.py` |
| Full render, ~40 s Short | ~2 min (Prompt 08) | measured in acceptance | full pipeline run, see `docs/ACCEPTANCE.md` |
| GET /health | — | 0.63 s cold / <0.05 s warm | curl, live server |
| GET /jobs | — | 0.015 s | curl, live server |
| GET /cost/summary | — | 0.023 s | curl, live server |

Full numbers and method in `docs/PERFORMANCE.md`.

## Security review (Phase 5)

- Secrets: env-only, none hardcoded (grep clean except
  tests/docs); `/settings/status` returns booleans + non-sensitive
  config only (tested); logs redact `api_key` / `token` /
  `Bearer` / webhook URLs (tested).
- Input: topics length-limited (200) and control-character
  stripped; job ids are UUIDs looked up, never interpolated.
- Files: `safe_join` rejects path traversal; downloads serve
  only files under the storage root (404 otherwise).
- Rate limiting: per-IP token bucket, 429 + Retry-After.
- Gaps: no authentication (see debt above); no CSRF concern
  (no cookies/sessions); no upload endpoint exists, so upload
  validation is `max_upload_mb` + `safe_join` only.

## Reliability review (Phase 4)

- Timeouts: per-attempt stage timeout (60 s default; 300 s for
  production runs), render 600 s, research 15 s/attempt.
- Retries: 3 attempts/stage with exponential backoff, except
  deterministic QA failures (attempted once; resume applies
  repairs, cap 2 rounds/job).
- Checkpoints: every completed stage persists; resume re-runs
  only missing stages.
- Backups: online SQLite backup + integrity check + pruning;
  PostgreSQL via pg_dump.
- Failure notifications: log + optional webhook, hooked into
  the engine's single failure funnel.
- Regression tests: `tests/test_deployment.py` (16 tests) +
  full suite (see `docs/FINAL_AUDIT.md`).
