# RANKFLOW AI — Complete System Integration (Prompt 10)

All seven agents are real and run as one autonomous workflow under
the central orchestrator. A user submits a topic and nothing else;
the system discovers sources, verifies rights, analyses footage,
ranks five moments from five different sources, plans the
countdown, renders the Short, QA-inspects it, and presents the
result for approval — or fails honestly at the stage that cannot
proceed, with a named recovery path.

## Compatibility inspection (Phase 1)

| Agent | Stage | Output schema | Executor | Health |
|---|---|---|---|---|
| Research | RESEARCH | ResearchOutput (≥1 candidate) | real, Internet Archive live | ready |
| Rights | RIGHTS_VERIFICATION | RightsOutput (≥1 approved) | real, CC catalogue + manual review | ready |
| Video Intelligence | VIDEO_ANALYSIS | VideoAnalysisOutput (≥1 moment) | real, ffprobe + OpenCV signals | ready |
| Ranking | RANKING | RankingOutput (exactly 5, 5 sources) | real, heuristic editorial scores | ready |
| Creative Director | CREATIVE_PLANNING | CreativeOutput (storyboard + EDL) | real, deterministic templates | ready |
| Video Production | VIDEO_PRODUCTION | ProductionOutput (MP4 1080×1920 @30fps) | real, FFmpeg | ready |
| Quality Assurance | QUALITY_ASSURANCE | QAOutput (verdict + issues) | real, ffprobe + OpenCV + audio | ready |

Findings:
- **Schemas align.** Every stage's executor output is validated by
  the engine against the stage's Pydantic schema in
  `app/orchestrator/contracts.py`; invalid output is a failure,
  never accepted.
- **Handoffs are validated and redundant.** The engine passes
  `candidate_ids → approved_candidate_ids → moment_ids →
  ranked_moment_ids → storyboard_id → rendered_video_id`
  between stages, and every agent also re-reads the database and
  re-checks the Rights gate before touching footage — a stale
  handoff cannot leak an unapproved source into a later stage.
- **No test stubs in the production path.** The default registry
  (built with an engine) registers only real executors;
  `test_doubles` exist only for orchestrator tests and are
  labelled as such. `tests/test_integration.py::test_all_seven_agents_are_real_and_ready`
  asserts this.
- **No agent was rewritten.** Prompt 10 changed the orchestrator
  (deterministic failures, repair resume), the QA executor's
  error type, and documentation only.

## End-to-end workflow (Phase 3)

```
POST /jobs {"topic": "..."}            # topic only — autonomous
POST /jobs/{id}/run
  RESEARCH -> RIGHTS_VERIFICATION -> VIDEO_ANALYSIS -> RANKING
  -> CREATIVE_PLANNING -> VIDEO_PRODUCTION -> QUALITY_ASSURANCE
  -> COMPLETED (job status: awaiting_approval, with QA report)
```

A plain run completes only when five eligible sources with
analysed moments exist. Otherwise it fails honestly at the stage
that cannot proceed (e.g. RANKING with one approved source, or
VIDEO_ANALYSIS with no local footage — downloading source videos
is not configured).

## Recovery (Phase 4)

| Failure | Behaviour |
|---|---|
| Failed search / provider timeout | Stage retried up to 3x with backoff; then job FAILED with the error stored; resume retries from the failed stage (checkpoints kept). |
| Rejected source / no licence | Rights marks it REJECTED / NEEDS_REVIEW; the job fails at RIGHTS_VERIFICATION unless ≥1 candidate is approved; a human supplies evidence via manual review, then resume. |
| Insufficient candidates | Ranking fails honestly (no moment invented). Recovery: approve more sources, then `POST /jobs/{id}/resume?rerun_from_stage=RIGHTS_VERIFICATION` — rights re-checks all candidates, video analysis re-runs, ranking gets five. Tested in `tests/test_integration.py`. |
| Incorrect timestamps | Video Intelligence re-verifies every timestamp against the original file; a mismatch fails the stage, never a silent wrong cut. |
| Script generation failure | Creative Director Phase 7 validation rejects the plan (forbidden word, missing moment, narration overrun); the stage fails, nothing is rendered. |
| Rendering failure | Production validates the render (ffprobe); an invalid render raises and the RenderedVideo row is left `failed`. |
| QA failure | **Deterministic — attempted exactly once, never retried** (Prompt 10 fix for the Prompt 09 issue). See Repair loop below. |
| API interruption | The failing stage's exception is stored on the job and in the execution log; the job is FAILED, never half-presented; resume continues from the first stage without a checkpoint. |

### Repair loop (QA)

1. QA persists a full report plus structured repair requests
   (`issue`, `responsible_component`, `correction`,
   `target_stage`, `target_agent`) — see `GET /jobs/{id}/qa`.
2. A `fail` verdict raises `DeterministicError`: the engine
   logs it once and fails the job. Re-inspecting the same render
   would fail the same way, so no retry is attempted.
3. `POST /jobs/{id}/resume` (default `apply_repairs=true`)
   reads the latest failed QA report, takes the earliest repair
   target stage, invalidates that stage's and all later stages'
   checkpoints (`JobService.invalidate_checkpoints`, one
   `repair_round` marker row per round), and re-runs from there
   — production re-renders from the approved EDL (the old
   RenderedVideo row is replaced), QA re-inspects the corrected
   render.
4. Repair rounds are capped at `qa_max_repair_attempts` (default
   2) **per job**, counted from the marker rows — the cap survives
   re-renders, unlike QA's per-render counter, which resets when
   Production replaces the RenderedVideo row. Past the cap,
   resume refuses with "repair attempts exhausted" and the job
   needs a human fix or a fresh job.
5. Repairs that target only `manual_review` / `config` (e.g.
   "configure a TTS provider") cannot be fixed by re-running
   anything: resume refuses and names the manual action instead
   of failing the same way again.

## Autonomous operation (Phase 5)

The only user input is the topic. No video, timestamp, ranking,
clip or setting is chosen by hand anywhere in the pipeline. When
the pipeline cannot proceed on its own — too few approved
sources, no licence evidence, no local footage — it stops safely
in FAILED with the missing input named in the error, and resumes
only after a human supplies it (manual review, more footage, or
configuration). Nothing is published automatically: a completed
job is `awaiting_approval` and the video is presented for review.

## End-to-end testing (Phase 6)

`tests/test_integration.py` (5 tests) — real agents throughout,
research on the test provider, five seeded CC-licensed clips
with local footage:
- all seven agents real/ready, no test stubs;
- topic-only run completes to `awaiting_approval` with a real
  1080×1920 render, five distinct sources in the ranking, and a
  `needs_review` QA report, also visible via the API;
- QA failure attempted once, then resume applies the repair and
  the corrected render passes QA inspection;
- insufficient-candidates failure recovers after human approval
  via resume from RIGHTS_VERIFICATION;
- resume refuses after the repair-round cap is reached.

Full suite: run `.venv/bin/python -m pytest tests/ -q` (renders
are CPU-slow on this VM — the integration tests add several
minutes; the suite is otherwise unchanged).

## Known limitations (unchanged from Prompts 08-09)

- No TTS provider is configured, so every real render has a
  silent narration track and its QA verdict is `needs_review`
  at best until a voice exists; QA cannot fix that (repair
  routes to `config` / manual review).
- No licensed music/SFX library is configured.
- No vision-language model and no OCR: narration accuracy vs
  picture, caption overlap, safe margins and unsuitable material
  are `not_tested`, never passed.
- The real parkour job still has only 1 Rights-approved
  candidate and 9 NEEDS_REVIEW — a full live end-to-end run on
  it is not possible until more candidates are approved via
  manual review. Do not bypass rights approval or invent sources.
- Runs are synchronous and in-process (no Redis/Celery broker);
  production runs need a 300 s orchestrator timeout, not the
  30 s default.
