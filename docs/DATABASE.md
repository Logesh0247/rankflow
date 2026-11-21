# Momentrix AI — Database Design (Prompt 01)

13 tables, generated from `app/models/models.py`.
PostgreSQL DDL: `app/database/schema.sql`.
Local testing: SQLite via `Base.metadata.create_all()`.

## Tables

| Table | Purpose | Key relationships |
|---|---|---|
| `jobs` | One topic request, its status, error and workflow stage | owns all rows below |
| `sources` | A video platform / channel | unique `(platform, external_id)` |
| `candidate_videos` | A discovered video for a job | `job_id`, `source_id` |
| `rights_evidence` | Licence / permission evidence per candidate | `candidate_video_id` |
| `approved_footage` | Footage cleared for use | `job_id`, `candidate_video_id`, `rights_evidence_id` |
| `analyzed_moments` | A moment inside a candidate video | `job_id`, `candidate_video_id` |
| `ranking_results` | Top-5 ranking | `job_id`, `moment_id`, `candidate_video_id` |
| `storyboards` | Countdown structure + narration script | `job_id` |
| `narrations` | Narration script + audio | `job_id`, `storyboard_id` |
| `edit_instructions` | Edit Decision List (JSON) + output size/fps | `job_id`, `storyboard_id` |
| `rendered_videos` | Final Short file + dimensions | `job_id`, `edit_instruction_id` |
| `qa_reports` | QA verdict + checks + issues | `job_id`, `rendered_video_id` |
| `agent_execution_logs` | Every agent run, status and duration | `job_id`, `agent_name` |

## Status vocabularies (enforced by CHECK constraints on Postgres)

- `jobs.status`: created, running, awaiting_approval, approved, failed, cancelled
- `jobs.current_stage` (added in Prompt 02, see `scripts/migrate_002_add_current_stage.py`
  for databases created before it): INITIALIZED, RESEARCH, RIGHTS_VERIFICATION,
  VIDEO_ANALYSIS, RANKING, CREATIVE_PLANNING, VIDEO_PRODUCTION, QUALITY_ASSURANCE,
  COMPLETED, FAILED, CANCELLED. A COMPLETED stage maps to `jobs.status`
  `awaiting_approval` — there is no JobStatus "completed". Checkpoints for
  each stage are the succeeded `agent_execution_logs` rows for that stage's
  agent, whose `detail` is JSON `{"output": {...}, "output_references": {...}}`.
- `candidate_videos.status`: discovered, rights_pending, rights_approved, rights_rejected, analyzed, selected, rejected
- `rights_evidence.verdict`: approved, rejected, needs_review, expired, not_checked (Prompt 04: `expired` added; new Phase 2 columns — `license_name`, `license_url`, `permission_evidence`, `verification_date`, `commercial_use`, `modification_allowed`, `redistribution_allowed`, `attribution_required`, `attribution_text`, `geographic_restrictions`, `time_restrictions`, `expires_at`, `verification_method`, `reviewer_status`, `reviewed_by`, `reviewed_at`, `reasons` — see `scripts/migrate_004_add_rights_fields.py` for databases created before it)
- `narrations.status`: draft, generated, approved
- `rendered_videos.status`: rendering, ready, failed
- `qa_reports.verdict`: pass, fail, needs_review, not_tested
- `agent_execution_logs.status`: started, succeeded, failed, skipped

## Ranking invariants

- `ranking_results.rank` is 1–5 (`CHECK rank BETWEEN 1 AND 5`).
- One row per `(job_id, rank)` and one row per `(job_id, moment_id)` —
  a job cannot rank the same moment twice, and cannot have two #1s.
- `ranking_results.score_breakdown` (Prompt 06, see
  `scripts/migrate_005_add_ranking_breakdown.py` for databases
  created before it) is the JSON component breakdown
  (entertainment, unexpectedness, visual_impact, context_clarity,
  technical_quality, total, all 0–100) behind the row's 0–1 `score`.
- Five unique sources is a *ranking-agent* rule (Prompt 06), not a
  database constraint: the DB guarantees five distinct moments; the
  agent must guarantee they come from five distinct
  `candidate_video_id`s / sources, and QA must verify it.

## Indexes

- `jobs(status, created_at)`, `candidate_videos(job_id, status)`,
  `analyzed_moments(job_id, candidate_video_id)`,
  `agent_execution_logs(job_id, agent_name)`, plus per-table
  job indexes on footage, rendered videos and QA reports.

## SQLite concurrency (2026-11-21 lock fix)

The live Render database locked ("database is locked") while a
long Archive footage download ran: the Video Intelligence
session held the first candidate's uncommitted writes across
the second candidate's slow download (one commit at the very
end), and every other write — new jobs, cancels, even recording
the failure — failed. Two changes, both in
`app/database/session.py` (`make_engine`, so every engine gets
them — `init_db.py`, `migrate_all.py`, uvicorn startup):

* **WAL mode** (`PRAGMA journal_mode=WAL`, stored in the file)
  — readers never block writers and writers never block
  readers.
* **Busy timeout 30 s** (`PRAGMA busy_timeout=30000`,
  per-connection) — a writer that still has to wait retries
  instead of failing instantly.

And a transaction rule every service follows: **no transaction
spans network I/O or a long render/download**. Video
Intelligence commits after each candidate; Rights commits
after each candidate; the footage downloader streams the file
first (no session open) and records `storage_path` in a short
session that commits immediately; Production renders with no
session open at all. The rule is proven by
`test_download_in_progress_does_not_block_other_writes`
(two candidates, second download slow, a job created
mid-download) and `test_sqlite_engine_uses_wal_and_busy_timeout`
in `tests/test_footage_download.py`. Backups use the sqlite3
online-backup API, which is WAL-safe — never copy the live
.db file by hand.
