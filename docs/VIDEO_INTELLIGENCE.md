# Video Intelligence Agent — Prompt 05

Analyses Rights-approved footage and identifies its most
interesting, visually clear, contextually understandable moments.

## Pipeline

1. **Rights gate first** — `app/agents/rights/gate.py:require_approved`
   runs before any file is touched. Unapproved, unknown, or
   other-job candidates raise a 422 and are never analysed.
2. **Ingestion** (`probe.py`) — ffprobe measures duration,
   resolution, frame rate, video/audio codecs and audio presence.
   Missing, unreadable, no-video-stream or zero-duration files are
   rejected with the measured reason.
3. **Frame extraction** (`frames.py`) — OpenCV samples frames
   (default 1 fps, adaptive down to at most 600 sampled frames for
   long videos) and measures brightness, sharpness (Laplacian
   variance), motion (mean absolute difference) and HSV-histogram
   correlation. Scene boundaries are where histogram correlation
   drops below 0.55.
4. **Analysis** (`analysis.py`) — segments the video at its scene
   boundaries, scores each segment, and returns at most 5 moments
   per video (default 2–12 s each; long segments are trimmed to
   their highest-motion window).
5. **Timestamp verification** (`verify.py`) — every moment's start,
   middle and end frames are re-read from the original file; 0.5 s
   lead-in and aftermath are added (clamped to the video and to
   neighbouring moments); truncated edges are flagged; overlapping
   moments are de-duplicated (IoU > 0.5, higher confidence wins);
   all-black moments are dropped.
6. **Persistence** — `AnalyzedMoment` rows (start/end, description,
   confidence as `score`, full measured signals in `labels`), a
   middle-frame thumbnail under
   `data/storage/jobs/<job_id>/moments/<moment_id>/thumb.jpg`,
   `ApprovedFootage.storage_path` / duration updated, candidate
   status set to `analyzed`. Re-running replaces that candidate's
   moments — no duplicates.

## The analyser — what it is and is not

The analyser is `heuristic_frame_signals_v1`: deterministic
frame signals only. **No vision-language or video-understanding
model is configured in this environment** (no key, no endpoint),
so the agent cannot name what happens in a scene. Moment
descriptions are factual signal labels ("scene segment 5.0–11.0s:
high-motion segment, mean frame difference 24.3…") and say so.
A semantic event label would be a fabrication and is never
produced. A real VLM can replace `analysis.py` later without
changing the moment schema — the `analyser` label in every
moment's `labels` records which one ran.

## Footage supply

The agent analyses local files. A file is resolved, in order,
from the `footage_paths` task input (`{candidate_id: path}`),
from `ApprovedFootage.storage_path`, or — when neither exists —
by downloading the approved source from the Internet Archive
(`download.py`, added after Prompt 12 to fix a live failure
where a job failed at VIDEO_ANALYSIS with no local file).

Download rules (all enforced, all honest):
- **Rights gate first, again.** The downloader re-checks
  `require_approved` server-side before every download.
  Rejected, needs-review, expired or never-checked footage is
  never downloaded.
- **Archive only.** The item identifier is the candidate's
  `external_video_id`; metadata comes from
  `{FOOTAGE_ARCHIVE_BASE_URL}/metadata/{id}` and the file from
  `{base}/download/{id}/{file}`. File selection prefers mp4,
  then webm, then mov, then the other playable containers;
  within one extension, known sizes first, largest first.
- **Caps.** Per-file cap `FOOTAGE_MAX_MB` (default 500) and
  per-job total cap `FOOTAGE_TOTAL_MAX_MB` (default 1024),
  enforced while streaming — an oversized file is aborted
  mid-download, never left half-written. A file over a cap is
  skipped with a clear reason.
- **Verify before use.** The downloaded file must probe with
  ffprobe (duration > 0); a file that fails verification is
  deleted, never analysed. `ApprovedFootage.storage_path` and
  duration are recorded only after a successful probe.
- **Fail soft per file.** One candidate failing to download
  (metadata 404, network error, corrupt file, over a cap) only
  skips that candidate; the next approved candidate still gets
  its chance. The stage fails only when no approved candidate
  produced a usable moment, naming the per-candidate reasons.
- **Resume works.** A downloaded file is recorded on the
  `ApprovedFootage` row, so a job that failed at VIDEO_ANALYSIS
  resumes from that stage (dashboard "Resume job" or
  `resume_job` / `rerun_from_stage=VIDEO_ANALYSIS`) and reuses
  the file instead of starting over. An already-present local
  file is never re-downloaded.
- **YouTube downloads are NOT in scope.** A non-Archive
  platform is skipped with a reason saying so. YouTube search
  itself is still unconfigured.

Set `FOOTAGE_DOWNLOAD_ENABLED=false` to restore the old
behaviour (approved candidates without a local file are
skipped). A large download can exceed the orchestrator's
per-attempt timeout (`ORCHESTRATOR_TIMEOUT_SECONDS`, default
60 s) — raise it when the per-file cap is large.

## API

- `GET /jobs/{job_id}/moments` — moments in time order
- `GET /jobs/{job_id}/moments/{moment_id}` — one moment, with
  `labels` carrying analyser, confidence, visual quality, context
  clarity, frame references, thumbnail and verification issues

## Demo

```bash
.venv/bin/python scripts/make_known_footage.py   # known footage + ground-truth manifest
.venv/bin/python scripts/run_video_intelligence.py <job_id> \
    --footage <candidate_id>=data/known_footage/known_scenes.mp4
```

## Known footage (Phase 7)

`data/known_footage/` (generated by `scripts/make_known_footage.py`,
manifest `manifest.json`):

- `known_scenes.mp4` — 30 s, scene boundaries at 5, 11, 20, 24 s;
  high-motion segments 5–11 s and 20–24 s.
- `known_black.mp4` — 12 s, 0–4 s black (must be dropped), 4–12 s
  moving. Boundaries are ground truth: chosen when the frames
  were drawn.

## Configuration

`VI_SAMPLE_FPS` (1.0), `VI_MAX_SAMPLED_FRAMES` (600),
`VI_SCENE_THRESHOLD` (0.55), `VI_MOTION_THRESHOLD` (6.0),
`VI_MIN_MOMENT_SECONDS` (2.0), `VI_MAX_MOMENT_SECONDS` (12.0),
`VI_MAX_MOMENTS_PER_VIDEO` (5), `VI_LEAD_IN_SECONDS` (0.5),
`VI_AFTERMATH_SECONDS` (0.5). Footage download:
`FOOTAGE_DOWNLOAD_ENABLED` (true), `FOOTAGE_ARCHIVE_BASE_URL`
(`https://archive.org`), `FOOTAGE_MAX_MB` (500),
`FOOTAGE_TOTAL_MAX_MB` (1024), `FOOTAGE_DOWNLOAD_TIMEOUT_SECONDS`
(300), `FOOTAGE_METADATA_TIMEOUT_SECONDS` (15).

## Known limitations

- No semantic event identification (no VLM) — ranking in Prompt 06
  scores these signal-based moments.
- Timestamps are accurate to about ±1 sampled frame (1 fps by
  default), then verified by re-reading the file.
- Long videos are sampled adaptively (≤ 600 frames), so a
  hours-long source is analysed coarsely, not frame-by-frame.
- Analysis is local-file only (downloaded Archive footage is a
  local file by the time it is analysed); no TTS/speech use.
- Archive file availability varies per item: some items publish
  no playable video file, and very large files are skipped by the
  size caps. YouTube downloads are not in scope.
