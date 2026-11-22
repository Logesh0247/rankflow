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
- **Archive.** The item identifier is the candidate's
  `external_video_id`; metadata comes from
  `{FOOTAGE_ARCHIVE_BASE_URL}/metadata/{id}` and the file from
  `{base}/download/{id}/{file}`. File selection prefers mp4,
  then webm, then mov, then the other playable containers;
  within one extension, known sizes first, largest first.
- **YouTube (Creative Commons only).** An approved YouTube
  video is downloaded with Vid Kraken first when
  `VIDKRAKEN_API_KEY` is configured: POST
  `https://vidkraken.com/api/v2/download` (`{"url", "format":
  "1080"}`, `Authorization: Bearer <key>`, Idempotency-Key),
  poll `GET /api/v2/download/{jobId}` until COMPLETED, then
  stream the returned `downloadUrl` — no cookies/proxies, which
  fixes the Render sign-in block. A job FAILED with a
  machine-readable `errorCode` (AGE_RESTRICTED, MEMBERS_ONLY,
  GEO_RESTRICTED, PRIVATE_VIDEO, DRM_PROTECTED,
  VIDEO_UNAVAILABLE, …) is a definitive failure and is never
  retried with yt-dlp. A pre-job HTTP 401/403 is an account
  rejection, not a video rejection — 401 is a missing/invalid
  key, 403 is a used-up plan allowance (live incident
  2026-11-21, job 348bbafa: 7 submit 403s, no clip ever
  downloaded) — and is returned as a definitive failure naming
  the cause (HTTP status + `code` from the `{"error", "code"}`
  body + plain meaning) instead of silently falling back to
  yt-dlp per candidate. When no key is set, or the Vid Kraken
  service fails / times out, yt-dlp is the fallback (`--format
  bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best`,
  `--merge-output-format mp4`, `--max-filesize` enforces the
  per-file cap). Only CC-licensed videos reach the downloader:
  Rights approves a YouTube video only on a Creative Commons
  licence or a recorded human permission grant, so a
  standard-licence video is never downloaded. yt-dlp runs as a
  subprocess with TLS verification left on; it is never run
  with certificate checks disabled. The binary is resolved from
  `FOOTAGE_YTDLP_PATH` on PATH, falling back to
  `python -m yt_dlp` when the package is importable. Free trial:
  10 Vid Kraken downloads, no card; then bandwidth-only pricing.
- **YouTube sign-in fallback.** YouTube demands a signed-in
  browser from some cloud servers — on 2026-11-21 every
  download from Render failed with "--from-browser or
  --cookies for the authentication", while search and Rights
  still worked. When yt-dlp fails that way (and only that
  way), the same download is retried with the mobile player
  clients (`--extractor-args
  youtube:player_client=android`, then `ios`). They need no
  account and no cookies — no cookies support exists anywhere
  in the downloader — and they often bypass the web sign-in
  check. Any other failure (video unavailable, over the size
  cap, timeout) returns immediately with no retry. Which
  player client produced a file is recorded on the download
  outcome (`FootageDownload.player_client`, also named in the
  reason when a fallback client was needed), so a job's
  diagnostics show whether the fallback was used. **Honest
  limit:** the player-client workaround is not guaranteed —
  YouTube can close it at any time, and it is verified only
  by a real Render-side job actually downloading a YouTube
  video. If it stops working, the fallback is Pixabay/Archive
  clips, which download as direct MP4 links and are not
  affected by YouTube's sign-in check.
- **Pexels / Pixabay (stock clips).** An approved stock clip is
  downloaded as a direct MP4 over HTTPS — no yt-dlp. The search
  response carries no file link, so the video is re-fetched by
  id from the provider API first (Pexels "get video by id" with
  the `Authorization` header, Pixabay `id` param with the `key`
  param), then the file link is selected (Pexels: largest mp4
  up to 1920 px wide; Pixabay: medium 1080p tier first) and
  streamed with the same caps. Only approved clips reach the
  downloader: Rights approves them on their platform licence
  (Pexels License / Pixabay Content License). A missing API key
  at download time is a clear failure, not a silent skip.
- **Caps.** Per-file cap `FOOTAGE_MAX_MB` (default 500) and
  per-job total cap `FOOTAGE_TOTAL_MAX_MB` (default 1024),
  enforced while streaming (Archive, Pexels, Pixabay) or by
  yt-dlp's `--max-filesize` (YouTube) — an oversized file is
  aborted, never left half-written. A file over a cap is skipped
  with a clear reason.
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
- **No transaction spans a download (2026-11-21 lock fix).**
  The file streams first with no database session open, and
  only afterwards a short session records `storage_path` +
  duration and commits immediately. Video Intelligence also
  commits after each candidate, so one candidate's writes
  never sit uncommitted through the next candidate's slow
  download. SQLite runs in WAL mode with a 30 s busy timeout
  (see `app/database/session.py` and `docs/DATABASE.md`).
- **Other platforms are out of scope.** TikTok, Facebook,
  Instagram and Pinterest offer no official search/download
  API, so a candidate from one of them is skipped with a reason
  saying so.

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
(300), `FOOTAGE_METADATA_TIMEOUT_SECONDS` (15),
`FOOTAGE_YTDLP_PATH` (`yt-dlp`).

## Known limitations

- No semantic event identification (no VLM) — ranking in Prompt 06
  scores these signal-based moments.
- Timestamps are accurate to about ±1 sampled frame (1 fps by
  default), then verified by re-reading the file.
- Long videos are sampled adaptively (≤ 600 frames), so a
  hours-long source is analysed coarsely, not frame-by-frame.
- Analysis is local-file only (downloaded Archive and YouTube
  footage is a local file by the time it is analysed); no
  TTS/speech use.
- Archive file availability varies per item: some items publish
  no playable video file, and very large files are skipped by the
  size caps. YouTube downloads cover CC-licensed videos only;
  a standard-licence video is never downloaded, and yt-dlp
  cannot reach YouTube from a host whose egress proxy blocks
  it (this VM) — live YouTube downloads are verified on the
  deploy host. YouTube can also demand a signed-in browser
  from cloud servers; the android/ios player-client fallback
  (see "Footage supply") is the no-account workaround, and it
  is not guaranteed — if it stops working, Pixabay/Archive
  clips (direct MP4 downloads) are the fallback. Pexels /
  Pixabay downloads are direct MP4 links and need their API
  keys configured at download time; live stock downloads are
  likewise verified on the deploy host.
