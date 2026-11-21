# RANKFLOW AI — Video Production Agent (Prompt 08)

Converts an approved Edit Decision List into a polished YouTube
Shorts video: asset preparation, FFmpeg clip extraction, 9:16
vertical reframing, narration, synchronised captions, motion
graphics, audio design, rendering, and render validation.

## Pipeline (9 phases, one EDL in, one MP4 out)

1. **Asset preparation** — retrieves the Creative Director plan
   (storyboard, narration, EDL, caption plan, visual direction),
   the ranked moments, and the approved footage. The Rights gate
   runs first and again per candidate: every clip's candidate must
   still be `APPROVED` and its footage file must exist on disk.
   A revoked approval, a missing file, a stale EDL timestamp, or
   a clip whose timestamps do not match its analysed moment fails
   the job honestly — no clip is rendered from an unapproved or
   unverified source.
2. **Clip extraction** — FFmpeg extracts each EDL segment with
   `-ss`/`-t` after the input, re-encodes at 30 fps, and validates
   start/end/duration (tolerance 0.25s, plus container rounding)
   and audio/video sync against the source probe before the clip
   is accepted.
3. **Vertical reframing** — every segment is cropped to 9:16 and
   scaled to 1080×1920. A motion centroid (OpenCV frame
   differencing over the clip) steers the crop window toward the
   action when motion is detected; otherwise a centre crop is
   used and the manifest says so. Safe margins keep text inside
   the frame; the crop window is always even-sized and inside
   the source.
4. **Narration** — the narration script from the Creative Director
   plan is synthesised to MP3 with **Edge TTS** (``edge-tts``,
   Microsoft Edge neural voices — free, no API key, no account)
   at the configured voice (default ``en-US-GuyNeural``,
   alternative ``en-US-JennyNeural``; `TTS_VOICE`,
   `TTS_PROVIDER=edge-tts`, `TTS_ENABLED=true`). The script is
   split into sentence chunks (≤900 chars) and each chunk is
   synthesised separately, then joined with FFmpeg into one MP3
   that starts at t=0 and is mixed over the full timeline as the
   primary track (music, when present, stays at low volume under
   it). The manifest records voice, duration, character count and
   chunk count with status ``generated``. **Failure is fail-soft:**
   if synthesis fails (endpoint down, rate-limited, or blocked —
   live synthesis is UNVERIFIED on this machine because the
   network proxy blocks `speech.platform.bing.com`), the render
   falls back to a silent narration track, the manifest records
   status ``generation_failed`` with the reason, and QA reports
   `needs_review` — a TTS failure never hard-fails a job. The
   narration script, word count and duration estimate are always
   in the manifest, and captions carry the words on screen.
   Commercial-use caveat: edge-tts is an unofficial client of
   Microsoft's endpoint with no SLA and possible rate-limiting —
   check the commercial-use terms before publishing narrated
   videos. If Edge TTS proves unreliable, the documented
   fallback is Piper (fully offline, needs a voice model
   downloaded, less natural voice).
5. **Captions** — the Creative Director caption plan is rendered
   as an ASS file per segment and burned in with libass
   (DejaVu Sans Bold, 54 px, centred bottom third, max 40
   characters per line, word emphasis via the plan's
   `emphasis_words`). Captions are re-timed to each segment's
   timeline start; a cue that falls outside its segment fails
   validation.
6. **Graphics** — drawtext overlays, all from the EDL/visual
   direction, no invented text: countdown number (`#5` … `#1`),
   ranking label (`MOMENT #5`), intro title card (topic +
   "TOP 5 COUNTDOWN"), outro card with the full attribution
   credits, and per-clip source attribution when the licence
   requires it. Fade in/out are 0.5s; hard-cut clips get no fade.
7. **Audio design** — there is **no licensed music or SFX library
   configured** (`PROD_MUSIC_DIR` / `PROD_SFX_DIR` empty), so the
   render has no background music and no mixed SFX; the manifest
   records `not_included_no_licensed_library` and never claims
   licensed audio was used. When a directory is configured, files
   are probed (must be audio) and the loudest track is mixed
   under the narration with ducking and loudnorm normalisation.
   SFX detection exists; SFX mixing is not implemented.
8. **Rendering** — FFmpeg concat demuxer (re-encode, not stream
   copy) to MP4, H.264 (libx264, CRF 20, yuv420p, faststart),
   AAC 128k stereo, 1080×1920 @ 30 fps. Transitions are explicit
   black gap segments of the EDL transition seconds between
   content segments, with cross-dissolve clips additionally fading
   into/out of the gap — not true xfades, which would overlap the
   segments and shorten the total below the EDL total. The
   rendered duration must match the EDL total within 0.6s or the
   render is rejected.
9. **Render validation** — ffprobe checks file integrity
   (decodable, no decode errors), duration, 1080×1920 resolution,
   30 fps, H.264 video stream, AAC audio stream. A report and a
   manifest (`manifest.json`) are written next to the MP4 with
   every segment, every asset status, warnings, and limitations.

## Output

- `storage/jobs/<job_id>/production/<render_id>/rankflow-short.mp4`
- `storage/jobs/<job_id>/production/<render_id>/manifest.json`
- Persisted `RenderedVideo` row (`status=ready`) and
  `ProductionReport` JSON; `RenderedVideo` is the orchestrator
  contract the QA stage will consume (`<job_id>-render`).

## API

- `GET /agents/production/health` → `ready` (FFmpeg present)
- `POST /jobs/{job_id}/production/run` → render (body may map
  `candidate_video_id` → local footage path)
- `GET /jobs/{job_id}/production` → render report + manifest
- `GET /jobs/{job_id}/production/file` → download the MP4

## Configuration

`PROD_MUSIC_DIR`, `PROD_SFX_DIR`, `PROD_FONT_FILE`,
`PROD_CAPTION_FONT_SIZE=54`, `PROD_CAPTION_MAX_CHARS=40`,
`PROD_MUSIC_DUCKING_DB=-10.0`, `PROD_LOUDNESS_TARGET_LUFS=-14.0`,
`PROD_RENDER_TIMEOUT_SECONDS=300.0`, `PROD_FFMPEG_TIMEOUT_SECONDS=120.0`,
`PROD_OUTPUT_FPS=30.0`, `PROD_CRITICAL_FAILURE_TIMEOUT_SECONDS=5.0`,
`TTS_PROVIDER=edge-tts`, `TTS_VOICE=en-US-GuyNeural`,
`TTS_ENABLED=true`, `TTS_TIMEOUT_SECONDS=120.0`
(see `.env.example`).

## Verification (Prompt 08)

Full suite: `.venv/bin/python -m pytest tests/ -q` — **210 passed,
0 failed** (176 before this prompt + 34 new production tests;
the full run took ~28 minutes on this VM because it renders six
real 1080×1920 videos on 1.5 shared CPUs). The production tests in
`tests/test_production.py` cover the crop math, subject-aware
reframing, extraction validation, caption ASS generation, graphics
filters, narration/music/SFX honest statuses, rendering to a real
MP4 (ffprobe-verified H.264/AAC 1080×1920 @ 30 fps, duration
matching the EDL), render validation, the rights gate (revoked
approval and missing footage rejected), stale-EDL rejection,
idempotency (second run returns the same render), the API, and a
full orchestrator run through real research → rights → video
intelligence → ranking → creative director → production →
quality assurance (the run ends `awaiting_approval` with a QA
report of `needs_review`, because no narration voice exists yet).
The older ranking and
creative-director orchestrator tests were updated the same way —
they previously expected the 501 at production.

A demo render is kept at `data/outputs/rankflow-short-demo.mp4`
with `data/outputs/rankflow-short-demo-manifest.json`. ffprobe:
H.264 1080×1920 @ 30 fps + AAC, 74.07s, 2.88 MB, validation
`valid: true`, 13 segments (intro + 5 moments + outro + 6
transition gaps). Its manifest states the honest limits in full:
narration `not_generated_no_tts_provider`, music and SFX
`not_included_no_licensed_library`.

## Known limitations

- Narration is synthesised with Edge TTS by default, but **live
  synthesis is unverified on this machine**: the network proxy
  blocks `speech.platform.bing.com`, so every real synthesis
  attempt here fails fast (~1s) and the render falls back to a
  silent narration track with the reason recorded. Verify live
  synthesis on the deploy host (Render) once its deploy is fixed.
  Tests use a stubbed synthesiser and never claim a live voice
  was heard.
- No licensed music/SFX library is configured, so renders have no
  background music and no mixed SFX. SFX files are detected when
  configured but never mixed into the render.
- Transitions are explicit black gaps, not true xfades — a
  deliberate trade-off to preserve the EDL total duration.
- Subject-aware reframing is a motion centroid from frame
  differencing, not object tracking; fast or off-centre action can
  still be cropped. No motion in a clip falls back to a centre
  crop and is flagged in the manifest warnings.
- Renders are slow on this VM: a ~40s Short takes roughly 2
  minutes of CPU (1080×1920 @ 30 fps, drawtext + scale per
  segment). The orchestrator timeout for production runs must be
  raised above the 30s default (300s is used in tests).
- Quality Assurance (Prompt 09) now inspects the render: a plain
  job run completes VIDEO_PRODUCTION, passes through QA and ends
  `awaiting_approval` with a QA report. In the test suite TTS is
  disabled, so the report is `needs_review` (narration missing,
  audio effectively silent) — see `docs/QUALITY_ASSURANCE.md`.
  With Edge TTS synthesising (verified with a stubbed synthesiser
  in `tests/test_production.py`), the same pipeline reaches QA
  `pass`: narration status `generated`, audio measurably
  non-silent.
