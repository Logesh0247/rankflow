# Prompt 09 — Quality Assurance Agent

Seventh real agent. Inspects the actual rendered video and decides
whether it is suitable for final user review. It never approves a
video that fails a mandatory check, and it never claims a check
passed when the check could not be performed.

## What it checks

### Phase 1 — File validation (`app/agents/quality_assurance/technical.py`)

ffprobe on the real file: integrity (probes without error, full
decode of every frame), resolution, aspect ratio, frame rate,
duration, video codec, audio codec, presence of an audio stream,
playback errors. Expected output is 1080×1920 @ 30 fps, H.264 +
AAC, duration matching the EDL within `qa_duration_tolerance_seconds`
(default 0.6 s). Any deviation is an **issue**.

### Phase 2 — Content validation (`app/agents/quality_assurance/content.py`)

Against the persisted Ranking and Creative Director rows and the
manifest:

- exactly five countdown-moment clips, in #5 → #1 order;
- five different source videos (no source reused);
- clip boundaries equal the analysed moment timestamps;
- no duplicate clips;
- narration present, fits the timeline, and contains no
  semantic-event word the footage signals cannot support
  (checked against the **per-moment** narrations only — the hook
  legitimately quotes the user's topic, e.g. "parkour fails");
- caption lines within 40 characters and inside the safe margins;
- rendered duration matches the EDL.

Content checks that cannot be performed without a vision model are
recorded as **not tested**, never passed: narration accuracy
against the picture, caption overlap with on-screen text, and
unsuitable visual material. The narration is checked only against
the signal-grounded narration contract — no vision-language
model exists, so no semantic event is claimed as verified.

### Phase 3 — Visual inspection (`app/agents/quality_assurance/visual.py`)

OpenCV sampling at 1 fps (cap 120 frames): black frames, frozen
frames inside countdown-moment windows, letterbox bands,
unexpected visual artifacts (large black borders), per-frame
brightness. Black frames inside known transition gaps are
expected and are not flagged — transitions are explicit black
gaps in the production render — and neither are black frames
inside the first/last 0.5 s, where Production fades the intro
in and the outro out. Caption overlap and text outside
safe margins are **not tested** (no OCR engine is configured).

### Phase 4 — Audio inspection (`app/agents/quality_assurance/audio.py`)

FFmpeg `volumedetect` + `loudnorm` on the real file: narration
clarity (measured level), music balance, clipping (max volume),
synchronization (audio duration vs video duration), sudden
volume changes, missing audio. Audio is **effectively silent**
when mean volume ≤ −70 dB — which is the state of every render
whose narration was not generated or failed to synthesise, since
no licensed music/SFX library exists either. QA flags that
honestly as an issue, never passes the missing audio as present.
A render with generated narration audio that is present and
non-silent clears this check.

### Phase 5 — Rights validation (`app/agents/quality_assurance/rights_check.py`)

Re-checks the live Rights evidence for every candidate in the
render at QA time (a licence revoked after production still
fails QA), requires attribution text for every attribution-
required candidate, and checks the manifest's music/SFX
attribution. Missing or expired permission is an **issue**.

### Phase 6 — Automated repair requests (`app/agents/quality_assurance/repairs.py`)

Each issue becomes a structured repair request:
`issue`, `responsible_component`, `target_stage`, `correction`
(a concrete fix the owning agent can act on), `retry_limit` (2).
Routing: resolution/codec/duration → `production` /
`VIDEO_PRODUCTION`; black/frozen frames → `production`;
narration wording/order → `creative_director`;
wrong source / rights → `rights`; narration/music missing
because no provider is configured → `config` /
`MANUAL_REVIEW` (no agent can fix a missing provider — a human
must configure one). No request is issued after
`qa_max_repair_attempts` (default 2) — later runs append
"repair attempts exhausted" and return `fail` with no further
requests, so a job cannot loop forever. The requests are
consumed by the orchestrator (Prompt 10, see
`docs/INTEGRATION.md`): a `fail` verdict raises
`DeterministicError` and is never retried; `POST
/jobs/{id}/resume` applies the requests, re-running from the
earliest target stage, capped at `qa_max_repair_attempts` repair
rounds **per job** (counted from the orchestrator's marker rows,
because the per-render counter here resets when Production
replaces the RenderedVideo row).

### Phase 7 — Final report (`app/agents/quality_assurance/service.py`)

```json
{"status": "PASSED", "issues": [], "warnings": [],
 "technical_validation": {}, "content_validation": {},
 "rights_validation": {}, "recommendations": []}
```

Verdict rules, implemented in `QAService._decide_verdict` and
recorded per check in the `checks` list:

- any **issue** → `fail` (the executor raises, so the orchestrator
  fails the job instead of presenting the video);
- no issues but any remaining issue-equivalent finding that is
  presentable for human review — in practice the missing
  narration voice / effectively silent audio — → `needs_review`
  (the executor returns it, the orchestrator completes the job
  as `awaiting_approval`, the human decides);
- only warnings → `pass` with warnings;
- no issues and no warnings → `pass`.

Every run persists a `QAReport` row (`verdict`, `issues`,
`warnings`, `technical_validation`, `content_validation`,
`visual_validation`, `audio_validation`, `rights_validation`,
`repair_requests`, `recommendations`, `checks` — the last is a
per-check list with verdicts `pass` / `fail` / `warning` /
`not_tested`, so a reviewer can see exactly which checks ran).

## Wiring

- `app/agents/quality_assurance/agent.py` — `QualityAssuranceAgent`
  (`AgentType.QUALITY_ASSURANCE`, `"quality_assurance"`) and
  `make_quality_assurance_executor(engine, settings)`. The executor
  returns `QAOutput` (`verdict`, `report_id`, `issues`, `warnings`,
  `repair_requests`, `recommendations`, `technical_validation`,
  `content_validation`, `rights_validation`) on `pass` /
  `needs_review`, and raises `ExternalServiceError` on `fail`.
- `app/orchestrator/registry.py` — the agent is registered with its
  executor and a real health check (probe + OpenCV importable).
- `main.py` — `GET /agents/quality_assurance/health`,
  `GET /jobs/{id}/qa`, `POST /jobs/{id}/qa/run`
  (422 when the latest report is `fail`), `GET /jobs/{id}/qa/history`.
- `app/api/quality_assurance.py` — the API routes.
- `app/agents/quality_assurance/service.py` — `QAService.run(job_id)`
  (full pipeline: rights re-check, creative plan, rendered video,
  manifest, inspect, persist) and `QAService.inspect_file(path,
  manifest=...)` for a standalone file, used by
  `scripts/run_qa.py` (`--job-id <id>` or `--file <path>
  [--manifest <path>]`, exit 0 pass, 1 fail, 2 needs_review).
- `app/core/config.py` — `qa_max_repair_attempts: int = 2`,
  `qa_sampling_fps: float = 1.0`,
  `qa_silence_threshold_db: float = -70.0`,
  `qa_duration_tolerance_seconds: float = 0.6`,
  `qa_loudness_min_lufs: float = -35.0`.
- `app/models/models.py` — `QAReport` table (7 columns: the full
  report JSON lives in `checks`, issues / warnings / repair
  requests in `issues`); `scripts/migrate_009_add_qa_report.py` adds it
  to Prompt 01 databases (`create_all()` already covers fresh
  databases).

## What the demo render passes and fails — honestly

`scripts/run_qa.py --file data/outputs/rankflow-short-demo.mp4
--manifest data/outputs/rankflow-short-demo-manifest.json`:

- **Passes:** technical validation (H.264 1080×1920 @ 30 fps +
  AAC, 74.07 s, decodes cleanly), content validation (five
  moments, five sources, correct order and boundaries, no
  duplicates, caption plan in bounds), visual inspection
  frames, no black/frozen/letterbox problems),
  rights validation (the demo's single approved source, CC BY
  3.0, with attribution).
- **Fails:** narration audio is not generated (no TTS provider)
  and the audio is effectively silent (mean volume −91 dB, the
  silent-track level); no licensed background music or SFX.
- **Verdict: `needs_review`** — presentable for a human to look
  at, not approved. A `pass` is reachable once a narration
  voice exists: the test suite proves it with a crafted render
  whose manifest says narration was generated and music is
  included (see `tests/test_quality_assurance.py::test_service_passes_crafted_render`),
  and with a real render whose narration was generated by a
  stubbed Edge TTS synthesiser (see
  `tests/test_production.py::test_service_renders_with_stubbed_narration_voice_and_passes_qa`).
  Live Edge TTS synthesis is still unverified on this machine.

## Tests

`tests/test_quality_assurance.py` — 31 tests,
covering valid and defective videos:

- file validation: valid 1080×1920 video passes; wrong
  resolution, wrong duration, corrupt file and missing file fail;
- visual: black frames detected, frozen moment detected, moving
  video passes, black frames inside transition gaps are not
  flagged, black frames inside the 0.5 s intro fade-in / outro
  fade-out are not flagged;
- audio: silent track detected as effectively silent, tone audio
  passes, clipping detected;
- content: five moments in order passes; wrong order, duplicate
  source, forbidden narration word and timestamp mismatch fail;
  the hook may quote the topic while moment narration may not
  claim an event;
- rights: unapproved candidate fails, approved + attributed passes;
- repairs: routing to the owning component, retry limit 2,
  exhaustion after `qa_max_repair_attempts`;
- service: crafted render passes; real ProductionService render
  is `needs_review` (narration missing, audio silent); executor
  raises on a failed render; unknown job / no render raise;
- demo render: `needs_review` with the honest failures above;
- API: health, get report, re-run, 404 for unknown job;
- orchestrator: full pipeline now completes to
  `awaiting_approval` with a QA report of `needs_review`.

Run the full suite with `.venv/bin/python -m pytest tests/ -q`.

## Known limitations

- No vision-language model exists, so narration accuracy against
  the picture, caption overlap and unsuitable-material checks are
  `not_tested` — recorded, never passed.
- No OCR engine exists, so text-outside-safe-margins is
  `not_tested` (caption plan bounds are checked from the EDL,
  not from pixels).
- Narration is synthesised with Edge TTS by default (free, no API
  key), but live synthesis is unverified on this machine — the
  network proxy blocks the Edge endpoint, so renders here fall
  back to a silent narration track (`generation_failed` in the
  manifest) and stay `needs_review` at best. A render with
  generated narration audio that is present and non-silent can
  reach `pass`. QA cannot fix a missing voice — the repair
  request routes to `config` / manual review for a missing
  provider, or to re-running production for a failed synthesis.
- No licensed music/SFX library is configured: the same applies
  to background music and mixed SFX.
- Visual inspection samples at 1 fps, so a defect shorter than
  a second between samples can be missed; frozen detection uses
  mean frame difference, so a very slowly moving scene could be
  misread as frozen (threshold 0.5 mean abs diff).
- Repair requests are structured and persisted, but no agent
  consumes them yet — re-running the job re-renders from the
  same EDL, so the same issue recurs until the underlying cause
  (usually the missing TTS/music provider) is fixed. That is
  Prompt 10 (Integration) territory, not this stage.
- The orchestrator does not yet retry QA `fail` automatically;
  the executor raising fails the job, which is the safe default.
