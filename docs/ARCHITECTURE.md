# Momentrix AI — Architecture (Prompt 01)

## Objective

Momentrix AI accepts a single topic, discovers eligible video sources,
identifies interesting moments, ranks five moments from five different
source videos, creates a professionally edited YouTube Short, validates
the output, and presents the completed video for user approval.

Prompt 01 builds only the foundation. Prompts 02–09 build the
orchestrator and the seven agents. Prompt 10 integrates them,
Prompt 11 builds the UI, Prompt 12 optimises and deploys.

## Environment findings (verified 2026-11-20, this VM)

| Item | Found | Version / note |
|---|---|---|
| Python | yes | 3.12.3 |
| FFmpeg / FFprobe | yes | 8.1.2, libx264 + AAC encoders present |
| OpenCV (headless) | installed in `.venv` | 5.1.0 |
| FastAPI / Uvicorn | installed in `.venv` | 0.165.0 / 0.54.0 |
| Pydantic / pydantic-settings | installed in `.venv` | 2.14.4 / 2.16.3 |
| SQLAlchemy | installed in `.venv` | 2.1.9 |
| psycopg (Postgres driver) | installed in `.venv` | 3.3.9 |
| redis client / Celery | installed in `.venv` | 8.1.0 / 5.7.0 |
| pytest / httpx | installed in `.venv` | 9.2.0 / 0.28.1 |
| SQLite | yes | system `sqlite3` binary + Python module |
| PostgreSQL server | **no** | no `psql`, `pg_isready`, or server socket |
| Redis server | **no** | no `redis-server` / `redis-cli` |
| YouTube API key | **no** | not configured, not verified |
| Muse AI API | **no** | named in the brief; no key / endpoint configured |
| TTS / speech APIs | **no** | not configured, not verified |
| S3-compatible storage | **no** | not configured, not verified |

Nothing above was assumed: every row was checked with `which`,
`python -c "import …"`, `ffmpeg -version`, or a live query, and is
reproducible with `scripts/check_env.py`.

## Architecture decisions

1. **Single modular Python service, not microservices.** One FastAPI
   app, one codebase, clear package boundaries (`core`, `api`,
   `database`, `models`, `schemas`, `storage`, `agents/*`). The
   brief says avoid unnecessary microservices; the seven agents are
   modules, not services, until a later prompt proves otherwise.
2. **FastAPI + Pydantic v2** for the API and for every agent contract.
   Agents exchange validated models, never ad-hoc dicts.
3. **SQLAlchemy 2.x** for persistence. Models are the single source
   of truth for the schema; `app/database/schema.sql` is generated
   from them in the PostgreSQL dialect, so the two cannot drift
   silently.
4. **PostgreSQL as the production target, SQLite as the local
   fallback.** The Postgres *driver* is installed, but no Postgres
   *server* exists in this environment, so local runs and tests use
   SQLite (`data/rankflow.db`). This is labelled as a fallback in
   code, config and docs — it is not presented as production storage.
   Switching is one env var: `DATABASE_URL`.
5. **Redis + Celery installed as libraries, not running.** No Redis
   server is present, so no broker is configured by default.
   `REDIS_URL` / `CELERY_BROKER_URL` stay empty until a server
   exists. Prompt 01 does not pretend background jobs work.
6. **Local filesystem storage first.** `LocalStorage` is the only
   implemented backend. S3 is an interface point: a future backend
   must implement the same `put / get_path / exists / delete`
   interface. No S3 credentials are read anywhere in Prompt 01.
7. **FFmpeg for video, OpenCV for frames.** Both are present and
   verified. No video code runs in Prompt 01 — the tools are
   checked by the health endpoint only.
8. **No agent code in Prompt 01.** Each `app/agents/<name>/`
   folder contains only a README stating which prompt builds it.
   There is no fake agent, no stub that claims to work.
9. **Configuration from environment only.** `pydantic-settings`
   reads `.env` / environment. No key is hardcoded. Secrets are
   never logged.
10. **Errors are typed** (`app/core/errors.py`) and mapped to HTTP
    codes by one handler in `main.py`.

## Data flow (target, not yet built)

```
topic → Job → Research → candidates → Rights → approved footage
      → Video Intelligence → moments → Ranking → top 5 (5 sources)
      → Creative Director → storyboard + narration → Production
      → rendered Short → QA → approval → publish
```

Every step writes to the database and to `agent_execution_logs`, so
a final video can be audited back to its source and rights check.

## Security

- Env-var config only; `.env` is never committed (see `.gitignore`).
- `safe_join` rejects path traversal outside the storage root.
- Input validation at the schema layer (Pydantic) and at the text
  layer (`clean_text`).
- Redirects disabled on authenticated requests (connector rule,
  applied in the mSightFlow helper pattern; no authenticated
  external call exists in Prompt 01).

## Known limitations

- No live PostgreSQL / Redis server — see the table above.
- No external API is connected or verified.
- No orchestrator, agents, video processing, TTS, captions or UI.
- SQLite does not enforce all PostgreSQL CHECK constraints the
  same way; the full DDL is in `schema.sql` for Postgres.
- `data/` (SQLite DB + local storage) is local state, not a backup.
