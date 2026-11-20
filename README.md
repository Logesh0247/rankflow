# RANKFLOW AI

Autonomous multi-agent video production platform.

**Prompt 10 — Complete System Integration complete (all seven
agents integrated).**

Accepts a single topic, discovers eligible video sources, identifies
interesting moments, ranks five moments from five different source
videos, creates a professionally edited YouTube Short, validates the
output, and presents the completed video for user approval.

> **Status after Prompt 10:** all seven agents are real, wired
> into the orchestrator, and proven end to end as one workflow
> (see `docs/INTEGRATION.md` and `tests/test_integration.py`).
> A topic-only `POST /jobs/{id}/run` goes through RESEARCH,
> RIGHTS_VERIFICATION, VIDEO_ANALYSIS, RANKING, CREATIVE_PLANNING,
> VIDEO_PRODUCTION and QUALITY_ASSURANCE and ends
> `awaiting_approval` with a QA report. A deterministic QA
> failure is attempted exactly once (never retried); resume
> applies QA's repair requests and re-renders, capped at 2 repair
> rounds per job, or re-runs from a named stage after human
> intervention (e.g. more sources approved). Research discovers
> candidates from the Internet Archive (verified live, no key);
> Research discovers candidates from the Internet
> Archive (verified live, no key); Rights checks licences — on
> the real parkour job it approved 1 of 10 candidates (CC BY 3.0)
> and marked 9 `needs_review` for having no licence. Video
> Intelligence probes approved footage with ffprobe, samples
> frames with OpenCV, segments at measured scene changes,
> detects moments, re-verifies every timestamp against the
> original file, and persists `AnalyzedMoment` rows with
> thumbnails. Its analyser is a deterministic heuristic over
> frame signals only — no vision-language model is configured,
> so no semantic event is inferred and no timestamp is
> fabricated. Ranking re-checks the Rights gate per candidate,
> scores every moment with `S = 0.25E + 0.25U + 0.20V + 0.15C +
> 0.15Q` from those same measured signals (scorer
> `heuristic_editorial_v1` — editorial heuristics, NOT
> viral-performance probabilities), drops duplicates (same
> moment, overlapping same-source moments, same-event
> re-uploads), keeps the best moment per source, and persists
> exactly five ranked moments from five different sources with
> the component breakdown and a written explanation. Fewer
> than five eligible sources fails the job honestly — no moment
> is invented. Creative Director re-checks the Rights gate per
> ranked candidate, scores several hook candidates and picks
> the strongest, and builds a #5-to-#1 countdown storyboard
> with signal-grounded narration (deterministic templates —
> no LLM, so no semantic event is ever claimed), a caption
> plan, visual direction and a validated Edit Decision List
> (1080×1920 @ 30 fps). `/agents/research/health`,
> `/agents/rights/health`, `/agents/video_intelligence/health`,
> `/agents/ranking/health`, `/agents/creative_director/health`,
> `/agents/production/health` and `/agents/quality_assurance/health`
> all report `ready`. A plain
> `POST /jobs/{id}/run` now gets past RESEARCH,
> RIGHTS_VERIFICATION, VIDEO_ANALYSIS, RANKING,
> CREATIVE_PLANNING, VIDEO_PRODUCTION and QUALITY_ASSURANCE
> (when five eligible sources with analysed
> moments exist — otherwise the ranking stage fails the job
> honestly) and ends `awaiting_approval` with a QA report.
> Quality Assurance inspects the real render (ffprobe + OpenCV +
> volumedetect/loudnorm, rights re-check, content checks) and
> reports `pass`, `needs_review` or `fail`. Video Production renders a real 1080×1920
> MP4 (H.264/AAC) from the approved EDL — see `docs/PRODUCTION.md`.
> Honest limits: no TTS provider is configured, so the render has
> no narration voice (silent narration track, script + captions
> carry the words); no licensed music/SFX library is configured,
> so there is no background music and no mixed SFX; transitions
> are explicit black gap segments, not true xfades. A search result is discovery only — reuse
> permission is established by Rights, never assumed, and
> unapproved footage is never analysed, ranked or used in a story.

**Prompt 11 — User Interface complete.** The RANKFLOW space is the
Creator Studio dashboard at `GET /ui/` (served by this same
backend, same origin — no simulated data): Home with topic input +
Generate, Production Dashboard with real stage progress, Final
Video Review with player + Approve / Reject & Regenerate, Job
History, and Settings (config status only, never secrets). It
matches the user's Creator Studio reference image (dark theme,
purple accent, sidebar, hero, Create with AI, 5-step workflow).
New endpoints: `POST /jobs/{id}/approve`,
`POST /jobs/{id}/reject` (re-renders from the approved EDL, does
not consume a QA repair round), `GET /settings/status`;
`JobRead` now exposes `current_stage`. See `docs/UI.md`.

**Prompt 12 — Optimization & Production Deployment complete.**
Measured performance: seek-based frame sampling (120 s clip
7.39 s → 3.04 s) and stream-copy concat (6.12 s → 0.89 s on a
13 s timeline), both verified by `scripts/benchmark.py`; DB
indexes added on storyboards / narrations / edit_instructions
(migration 011). Cost monitoring: `cost_events` table,
`CostService`, `GET /jobs/{id}/cost` and `GET /cost/summary`
(compute $0.10/CPU-hour, storage $0.023/GB-month; TTS and
licensed audio are $0 because those features are absent, not
free). Reliability: online DB backups (`scripts/backup_db.py`),
storage validation at startup and at `GET /health/detailed`,
failure notifications (log + optional webhook), deterministic
QA failures never retried. Security: per-IP rate limiting
(600/min default), secret redaction in logs, downloads confined
to the storage root. Deployment: `Dockerfile`,
`docker-compose.yml`, `scripts/migrate_all.py`,
`docs/DEPLOYMENT.md`. Final acceptance test on
"TOP 5 CRAZIEST PARKOUR FAILS" (`scripts/run_acceptance.py`):
**9 PASS, 1 PARTIAL, 0 FAIL** — check (9) QA is PARTIAL
(`needs_review`: no TTS voice, audio effectively silent), all
other checks pass; full detail in `docs/ACCEPTANCE.md` and
`docs/FINAL_AUDIT.md`.

## Quick start

```bash
cd ~/workspace/rankflow_ai
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # edit only if you need Postgres / Redis
.venv/bin/python scripts/init_db.py
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

Then:

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"topic": "funniest parkour fails"}'
# Simulated end-to-end run (test doubles only — orchestrator testing, not video production):
curl -X POST http://127.0.0.1:8000/jobs/<id>/run?use_test_doubles=true
curl http://127.0.0.1:8000/jobs/<id>/history
# Real agents (all seven, integrated in Prompt 10): live discovery, then rights check, then analysis, then ranking, then creative planning, then a real MP4 render, then QA
curl -X POST http://127.0.0.1:8000/jobs/<id>/run   # end to end: research + rights + video_intelligence + ranking + creative_director + production + quality_assurance -> awaiting_approval
# If the job fails at QA, resume applies the repair requests and re-renders (max 2 repair rounds):
curl -X POST "http://127.0.0.1:8000/jobs/<id>/resume"
# If a human approved more sources after an insufficient-candidates failure, resume from rights:
curl -X POST "http://127.0.0.1:8000/jobs/<id>/resume?rerun_from_stage=RIGHTS_VERIFICATION"
curl http://127.0.0.1:8000/jobs/<id>/candidates
curl http://127.0.0.1:8000/jobs/<id>/rights
curl http://127.0.0.1:8000/jobs/<id>/attribution
curl http://127.0.0.1:8000/jobs/<id>/moments
curl http://127.0.0.1:8000/jobs/<id>/ranking
curl http://127.0.0.1:8000/jobs/<id>/storyboard
curl http://127.0.0.1:8000/jobs/<id>/narration
curl http://127.0.0.1:8000/jobs/<id>/edl
```

## Creative Director demo

```bash
.venv/bin/python scripts/run_creative_director.py <job_id>
```

Builds the countdown storyboard, hook, narration, caption plan,
visual direction and Edit Decision List from the job's five
ranked moments. Fails honestly when fewer than five moments are
ranked, or a ranked candidate is no longer Rights-approved — no
moment is invented. See `docs/CREATIVE_DIRECTOR.md`. Narration
is built from measured frame signals only — no semantic event
is ever claimed.

## Ranking demo

```bash
.venv/bin/python scripts/run_ranking.py <job_id>
```

Ranks the job's analysed moments and prints the five selected
moments (#1 best first) with component scores and explanations.
Fails honestly when fewer than five eligible sources exist — no
moment is invented. If your database was created before Prompt 06,
run the migration first:
`.venv/bin/python scripts/migrate_005_add_ranking_breakdown.py`

## Video Intelligence demo

```bash
.venv/bin/python scripts/make_known_footage.py   # known footage + ground-truth manifest
.venv/bin/python scripts/run_video_intelligence.py <job_id> \
    --footage <candidate_id>=data/known_footage/known_scenes.mp4
```

Analyses every Rights-approved candidate of the job that has a
local footage file (from `ApprovedFootage.storage_path` or
`--footage`). See `docs/VIDEO_INTELLIGENCE.md`. The analyser is a
deterministic heuristic over frame signals only — no
vision-language model is configured, so moments are described by
their measured signals, never by an inferred event.

## Rights demo

```bash
.venv/bin/python scripts/run_rights.py <job_id>
```

Verifies every candidate Research persisted for a job and prints
the verdicts with reasons. If your database was created before
Prompt 04, run the migration first:
`.venv/bin/python scripts/migrate_004_add_rights_fields.py`

## Research demo

```bash
.venv/bin/python scripts/run_research.py "TOP 5 CRAZIEST PARKOUR FAILS"
```

Runs the real Research Agent for one topic and prints the search
plan, candidates with scores, metrics and limitations. If your
database was created before Prompt 03, run the migration first:
`.venv/bin/python scripts/migrate_003_add_candidate_metadata.py`

## Orchestrator demo

```bash
.venv/bin/python scripts/run_job.py "TOP 5 CRAZIEST PARKOUR FAILS"
```

Runs a clean simulated job through all workflow states, then a
failure-recovery run (ranking fails 3 times, job FAILED, resume
completes it from the failed stage). If your database was created
by Prompt 01, run the migration first:
`.venv/bin/python scripts/migrate_002_add_current_stage.py`

## Environment inspection

```bash
.venv/bin/python scripts/check_env.py
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

## Project structure

```
app/
  core/          config, logging, errors, security
  orchestrator/  workflow engine, registry, job service, TEST-ONLY doubles (Prompt 02)
  agents/        research BUILT (Prompt 03); rights BUILT (Prompt 04);
                 video_intelligence BUILT (Prompt 05);
                 ranking BUILT (Prompt 06);
                 creative_director BUILT (Prompt 07);
                 production BUILT (Prompt 08);
                 quality_assurance NOT BUILT — Prompt 09
                 (one README per agent)
  api/           health + jobs + orchestrator + research + rights + video_intelligence + ranking + creative_director + production endpoints
  database/      SQLAlchemy session + schema.sql
  models/        13 SQLAlchemy tables
  schemas/       shared Pydantic contracts
  services/      (empty — Prompt 02+)
  integrations/  integration points, not implemented
  storage/       local filesystem backend
  utils/         ffmpeg / path helpers
tests/ docs/ scripts/ config/ main.py requirements.txt .env.example
```

## Documentation

- `docs/ARCHITECTURE.md` — decisions, environment findings, limitations
- `docs/DATABASE.md` — tables, relationships, status vocabularies
- `docs/CONTRACTS.md` — shared Pydantic models
- `docs/ORCHESTRATOR.md` — workflow, contracts, reliability, tests (Prompt 02)
- `docs/RESEARCH.md` — Research Agent, providers, scoring, live measurements (Prompt 03)
- `docs/RIGHTS.md` — Rights Verification Agent, licence catalogue, enforcement, live measurements (Prompt 04)
- `docs/VIDEO_INTELLIGENCE.md` — Video Intelligence Agent, frame signals, timestamp verification, live measurements (Prompt 05)
- `docs/RANKING.md` — Ranking Agent, scoring formula, duplicate detection, insufficient-candidate handling (Prompt 06)
- `docs/CREATIVE_DIRECTOR.md` — Creative Director Agent, hooks, signal-grounded narration, storyboard, EDL, validation (Prompt 07)
- `docs/PRODUCTION.md` — Video Production Agent, FFmpeg pipeline, reframing, captions, graphics, audio, rendering, validation (Prompt 08)

## Known limitations (Prompts 01–08)

- No PostgreSQL or Redis server in this environment — SQLite is the
  local-testing fallback, labelled as such. Postgres is configured
  by `DATABASE_URL` when a server exists.
- No YouTube / Muse AI / TTS / speech / S3 integration is configured
  or verified. YouTube discovery exists as code but is disabled
  without a `YOUTUBE_API_KEY`; the live discovery provider is the
  Internet Archive (public archive, not a YouTube replacement —
  relevance ≈ 0.52 for broad topics, 0.84 for exact ones).
  Video Production has no TTS provider and no licensed music/SFX
  library configured: renders are silent except for source-clip
  audio, with no narration voice and no background music — see
  `docs/PRODUCTION.md` for the exact labelled fallbacks.
- Most Internet Archive items publish no licence, so Rights marks
  them `needs_review` — by design. A job with no licenced
  candidates fails at rights until a human supplies evidence via
  manual review.
- Quality Assurance (Prompt 09) inspects the rendered MP4 and
  reports `pass` / `needs_review` / `fail`; a plain job run now
  renders a real MP4 at VIDEO_PRODUCTION, passes QA and ends
  `awaiting_approval` with a QA report. The orchestrator runs end
  to end with the real agents.
