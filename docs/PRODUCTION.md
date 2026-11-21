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
4. **Narration** — **off by design in the full-bleed style**
   (2026-11-21 restyle): the reference videos carry the text on
   screen, not as narration, so no TTS synthesis runs and the
   render's audio track is the source clips' own audio. The Edge
   TTS code (`edge-tts`, Microsoft Edge neural voices — free, no
   API key, no account; `TTS_PROVIDER=edge-tts`,
   `TTS_VOICE=en-US-GuyNeural`, `TTS_ENABLED=true`) is intact,
   just not called in this style path, and can be re-enabled by a
   future style that wants a voiceover. The narration script from
   the Creative Director plan still exists, its word count and
   duration estimate are in the manifest with status
   ``not_generated_voiceover_off_fullbleed_style``, and captions
   carry the words on screen. QA therefore reports
   `needs_review` for every full-bleed render — the honest verdict
   for a voiceless video. Commercial-use caveat (if TTS is ever
   re-enabled): edge-tts is an unofficial client of Microsoft's
   endpoint with no SLA and possible rate-limiting — check the
   commercial-use terms before publishing narrated videos. If Edge
   TTS proves unreliable, the documented fallback is Piper
   (fully offline, needs a voice model downloaded, less natural
   voice). Live synthesis is UNVERIFIED on this machine because
   the network proxy blocks `speech.platform.bing.com`.
5. **Captions** — the Creative Director caption plan is rendered
   as an ASS file per segment and burned in with libass
   (DejaVu Sans Bold, 54 px, centred, max 40
   characters per line, word emphasis via the plan's
   `emphasis_words`). Captions are re-timed to each segment's
   timeline start; a cue that falls outside its segment fails
   validation. In the full-bleed style the ASS captions are
   raised (MarginV 380) so they sit above the big bottom caption
   instead of colliding with it.
6. **Graphics** — full-bleed drawtext overlays, all from the
   EDL/visual direction and the job topic, no invented text. The
   clip fills the whole 1080×1920 frame (reframed with the same
   motion-centroid subject-aware crop, no padding, no dark
   background). A solid black title bar (y 0..200) carries the
   job topic split into two lines: "RANKING" white plus a
   qualifier phrase (e.g. "THE CRAZIEST") red `#ff2d2d` on line 1,
   the remaining subject words yellow `#ffd400` on line 2, all
   big bold stroked text (84px falling back to 72/56px, 8px
   black stroke). Down the left edge, numbers 1. to 5. (120px,
   10px black stroke) stay on screen the whole video; the number
   of the clip currently playing is yellow, the rest white, and
   each number's short per-clip caption (48px, white with black
   outline) appears next to it once its clip has played. Across
   the bottom, a big bold uppercased caption (64px, 8px black
   stroke, up to two lines) carries the current clip's caption.
   Text is measured with Pillow against the same DejaVu Sans Bold
   font FFmpeg draws with, so titles wrap and centre exactly.
   Straight cuts between clips, no black gap, no intro/outro
   cards. Per-clip source attribution is shown in the lower third
   for the last 2 seconds of its segment when the licence
   requires it; there is **no end-credits screen** — the full
   attribution for every clip is in the render manifest and in
   the report's `description_attribution`, to be pasted into the
   video description when publishing. Honest limit: the style
   matches the reference parkour ranking videos' layout, but the
   clips are auto-selected by Momentrix from licensed sources —
   unlike those videos, the user does not hand-pick or order
   them, and the list labels reuse the Creative Director's
   signal-grounded narration text, not hand-written clip
   descriptions.
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
   AAC 128k stereo, 1080×1920 @ 30 fps. Clips are joined with
   straight cuts — no black gap segments, no intro/outro cards,
   no fades. The rendered duration must match the EDL total
   (the five clip durations only) within 0.6s or the render is
   rejected.
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
`not_included_no_licensed_library`. **This demo predates the
full-bleed restyle (2026-11-21) and still shows the old Rankter
layout; the current pipeline renders 5 segments (the five clips
only, straight cuts) with narration
`not_generated_voiceover_off_fullbleed_style`.**

## Known limitations

- Narration is **off by design in the full-bleed style** — the
  reference videos carry the text on screen, not as narration —
  so no TTS synthesis runs and every render's QA report is
  `needs_review`. The Edge TTS code is intact and can be
  re-enabled by a future style; live synthesis is unverified
  on this machine (the network proxy blocks
  `speech.platform.bing.com`). Tests use a stubbed synthesiser
  and never claim a live voice was heard.
- No licensed music/SFX library is configured, so renders have no
  background music and no mixed SFX. SFX files are detected when
  configured but never mixed into the render.
- Clips are joined with straight cuts — no black gaps, no
  intro/outro cards, no fades — to match the reference videos.
  There is no end-credits screen: attribution lives in the
  manifest and in the report's `description_attribution` (paste
  it into the video description when publishing), plus a small
  on-screen attribution line in the last 2 seconds of each
  segment whose licence requires it.
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
  non-silent. That path is currently disabled: in the full-bleed
  style the voiceover is off by design, so the pipeline always
  ends `needs_review` on narration.
