# RANKFLOW AI — Deployment Guide (Prompt 12, Phase 6)

## What this service actually needs

Measured on this machine (1.5 vCPU, 6 GB RAM, no GPU):

| Resource | Requirement | Evidence |
|---|---|---|
| CPU | 2 vCPU minimum, 4 recommended | FFmpeg renders are CPU-bound: ~2 min per ~40 s Short here; frame analysis is OpenCV on CPU |
| Memory | 4 GB minimum | Renders + OpenCV sampling peak well under 2 GB for 1080×1920 |
| Disk | 20 GB free minimum (500 MB is the startup warning floor) | One job: ~3–15 MB render + segments + thumbnails; DB is < 1 MB at current scale |
| Network | Outbound HTTPS to archive.org (Research) | Internet Archive is the only live provider; no other egress is required |
| GPU | None | No model runs locally; no vision-language model is configured |

**Hosting choice:** any small VPS or container host with 2 vCPU / 4 GB
(DigitalOcean $12/mo droplet, Hetzner CX22, AWS t3.medium, or
equivalent) is enough for one job at a time. Jobs run
synchronously in-process — there is no worker queue. Different
jobs can run concurrently (each opens its own database session
and checkpoints per job), but they share the same CPU, so two
renders at once each run roughly twice as slow; the same job
must never be run twice concurrently. Run one API worker: there
is no distributed lock, and the in-memory rate limiter does not
span workers.

## Deploy with Docker (recommended)

```bash
cp .env.example .env        # fill in only what you use; leave the rest commented
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

The compose file binds the API to **127.0.0.1:8000 only**. The
service has no login of its own — never expose it directly to the
internet. Put it behind a reverse proxy (Caddy / nginx) with
authentication, e.g. Caddy `basic_auth`, and TLS.

```bash
# Verify the deployment
curl http://127.0.0.1:8000/health/detailed
curl http://127.0.0.1:8000/ui/     # the RANKFLOW space
```

## Deploy without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python scripts/migrate_all.py   # idempotent; safe on every deploy
.venv/bin/python scripts/init_db.py
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

Run it under systemd (or supervisord) with `Restart=on-failure`.
One worker only, for the reason above.

## Database

- Default: SQLite at `data/rankflow.db` (local fallback).
- Production target: PostgreSQL 16.
  Set `DATABASE_URL=postgresql+psycopg://user:password@host:5432/rankflow`
  and apply `app/database/schema.sql` once, then
  `scripts/migrate_all.py` on every deploy.
- Migrations: `scripts/migrate_*.py`, run in order by
  `scripts/migrate_all.py`. Every migration is idempotent — it
  checks whether its change already exists. The script stops on
  the first failure; never ignore a failed migration and start
  the app anyway.

## Backups and recovery

```bash
# SQLite (online backup, safe while the app runs; integrity-checked)
.venv/bin/python scripts/backup_db.py            # -> data/backups/rankflow-<UTC>.db
.venv/bin/python scripts/backup_db.py --keep 14  # keep 14 (default 7)

# PostgreSQL: the same script uses pg_dump.
```

Schedule it — cron, once a day is enough at current volume:

```
0 3 * * * cd /opt/rankflow && .venv/bin/python scripts/backup_db.py >> data/backups/backup.log 2>&1
```

**Restore (SQLite):** stop the app, replace `data/rankflow.db`
with the backup file, restart. Verify with
`sqlite3 data/rankflow.db "PRAGMA integrity_check;"` — it must
print `ok`. Rendered MP4s live under `data/storage/jobs/...`;
back up that directory too if you want old videos restorable
(the database only stores their paths — a missing file is
reported by `GET /health/detailed`, it does not crash the API).

## Storage

- Local filesystem under `STORAGE_ROOT` (`data/storage` by
  default). S3 is an interface point only — `storage_backend: s3`
  is not implemented; do not set it.
- Startup validates the storage root (exists, writable), the
  database file, every rendered-video path in the DB, and free
  disk space (warns below 500 MB free). It logs warnings; it
  never refuses to boot.
- `GET /health/detailed` reports the same checks live, plus the
  five most recent failure notifications.

## Logging and monitoring

- Logs: plain text to stdout, one line per event
  (`timestamp level logger message`). Run under systemd/Docker
  and collect stdout; there is no log file by default.
- Secrets are redacted in log output (`api_key=***`,
  `Bearer ***`, webhook URLs masked). Never log a full request
  body or a file's contents.
- Health: `GET /health` (fast, no writes) for load-balancer
  checks; `GET /health/detailed` for storage + recent failures.
- Cost: `GET /jobs/{id}/cost` and `GET /cost/summary` — measured
  compute seconds per stage, API calls, live storage size, and
  estimated dollars at the documented rates in
  `app/core/cost.py` ($0.10/CPU-hour compute, $0.023/GB-month
  storage). TTS and licensed audio are $0 because those features
  are absent (no provider / no library), not because they are
  free — the moment a TTS provider is configured, its rate goes
  in `app/core/cost.py` and events are recorded.
- Failure notifications: every job failure is appended to
  `data/notifications.log` and, when `NOTIFY_WEBHOOK_URL` is set,
  POSTed to that webhook. The webhook URL is a secret — it lives
  in the environment only and is never returned by any endpoint.

## Security checklist (before exposing anything)

1. Bind to 127.0.0.1 and put a reverse proxy with authentication
   in front. This service has no login.
2. Keep `.env` out of git and out of the Docker image (the
   Dockerfile deletes it; `.dockerignore` excludes it).
3. `RATE_LIMIT_PER_MINUTE` — 600 by default; set 120 when the
   API is proxied. The limiter is in-memory per process; a
   multi-worker deployment must move it to Redis.
4. Downloads (`GET /jobs/{id}/rendered-video/download`) serve
   only files under the configured storage root; paths outside
   it return 404.
5. Topics are length-limited (200 chars) and stripped of control
   characters; uploaded/generated files must stay inside the
   storage root (`safe_join` rejects traversal).
6. `/settings/status` returns booleans and non-sensitive config
   only — verified by `tests/test_deployment.py`.
7. Secrets come from environment variables only. Nothing is
   hardcoded; `grep -r "sk-\|api_key.*=.*['\"]"` the tree before
   every release (there should be no hits outside tests/docs).

## Reliability

- **Timeouts:** per-attempt stage timeout
  (`ORCHESTRATOR_TIMEOUT_SECONDS`, default 60 s; production
  runs need 300 s), render timeout 600 s, research 15 s per
  attempt. A timeout is a stage failure, never a hang.
- **Retries:** up to 3 attempts per stage with exponential
  backoff — except deterministic failures (QA `fail`), which are
  attempted exactly once; recovery is via resume, which applies
  QA's repair requests (cap 2 rounds per job).
- **Checkpoints:** every completed stage persists its output;
  resume re-runs only stages without a checkpoint.
- **Regression tests:** `tests/test_deployment.py` (cost,
  rate limiting, secret redaction, backup, storage validation,
  notifications, migrations) plus the full suite
  (`.venv/bin/python -m pytest tests/ -q`).

## Known limits (do not deploy past these without fixing them)

- No TTS provider: every real render is `needs_review` at best
  until a narration voice exists.
- No licensed music/SFX library: renders have no music or SFX.
- No vision-language model / OCR: narration accuracy vs picture,
  caption overlap, safe margins and unsuitable material are
  `not_tested` in QA.
- One job at a time per host is the practical limit (CPU-bound
  renders); different jobs can run concurrently but share the
  CPU. One API worker; no queue. Redis + Celery are
  installed as libraries but no broker is configured.
- SQLite is fine for a single host; move to PostgreSQL before
  running more than one host or more than a handful of jobs a day.
- The real parkour job still has only 1 Rights-approved
  candidate (9 `needs_review`) — a full live end-to-end run on
  it is not possible until more candidates are approved via
  manual review. Do not bypass rights approval.
