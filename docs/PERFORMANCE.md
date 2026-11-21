# Momentrix AI — Performance (Prompt 12, Phase 2)

Measured on this machine (1.5 vCPU, 6 GB RAM, no GPU) with
`scripts/benchmark.py`, which uses real files and wall-clock time.
Run it yourself:

```bash
.venv/bin/python scripts/benchmark.py
```

## Baseline (before Prompt 12) vs after

| Path | Before | After | Speedup |
|---|---|---|---|
| Frame extraction, 30 s clip (320×240, 30 fps) | 0.272 s | 0.145 s | 1.9× |
| Frame extraction, 120 s clip (640×360 testsrc) | 7.391 s | 3.036 s | 2.4× |
| Concat, 13 s timeline (3 segments) | 6.124 s | 0.893 s | 6.9× |

Full benchmark output (after):

```json
{
  "frames": {
    "known_30s": {"seconds": 0.145, "samples": 30, "effective_fps": 1.0,
                  "scene_boundaries": [5.0, 11.0, 20.0, 24.0]},
    "synthetic_120s": {"seconds": 3.036, "samples": 120, "effective_fps": 1.0,
                       "scene_boundaries": []}
  },
  "concat": {
    "reencode_seconds": 6.124, "stream_copy_seconds": 0.893,
    "speedup": 6.86, "reencode_valid": true, "stream_copy_valid": true,
    "stream_copy_issues": [], "reencode_bytes": 269146,
    "stream_copy_bytes": 263394
  }
}
```

## What changed and why it is safe

1. **Seek-based frame sampling** (`app/agents/video_intelligence/frames.py`).
   The old code decoded every frame of a video to sample 1 fps —
   30 decoded frames per sampled frame. The new code seeks
   straight to each sampled frame index (`CAP_PROP_POS_FRAMES`)
   and measures the same frame. The sampled frames are identical:
   the 30 s known-footage clip still yields exactly 30 samples
   with scene boundaries at 5, 11, 20, 24 s, and
   `tests/test_video_intelligence.py` (26 tests) passes unchanged.
   If any seek/read fails, the reader falls back to the
   sequential scan. Why only 2.4× on the 120 s clip: OpenCV
   seeks decode forward from the nearest keyframe (GOP 60 here),
   so a seek still decodes up to 60 frames.
2. **Stream-copy concat** (`app/agents/production/render.py`).
   Segments are already uniform 1080×1920 @ 30 fps H.264/AAC,
   so the final join re-encoded the video a second time for
   nothing. Now the video stream is copied and only the audio
   is re-encoded through the loudnorm/fade chain. The output is
   byte-identical in size class (263,394 vs 269,146 bytes) and
   still passes Phase 9 validation (ffprobe + full decode).
   If stream-copy fails (e.g. a segment's codec parameters
   differ), the render falls back to the full re-encode instead
   of failing the job. `tests/test_production.py` (34 tests)
   passes with the new path.
3. **Database indexes** (migration 011). `storyboards`,
   `narrations` and `edit_instructions` had no `job_id` index.
   `EXPLAIN QUERY PLAN` now shows index search on every hot
   lookup (`candidate_videos`, `agent_execution_logs`,
   `storyboards`, `cost_events`).

## What was measured and deliberately NOT changed

- **Clip extraction** (`extract_clip`): still re-encodes each
  clip with accurate seek after `-i`. A fast keyframe seek
  would be quicker but is not frame-accurate, and the EDL
  timestamps require frame accuracy. Left alone.
- **QA sampling**: 1 fps, max 120 frames — already bounded.
- **API reads**: `GET /jobs` 0.015 s, `GET /cost/summary`
  0.023 s, `GET /ui/` 0.050 s on the live server. No API
  optimisation was needed; the slow paths are all video work.
- **No AI calls to avoid**: there is no LLM/VLM/TTS call
  anywhere in the pipeline — the only external calls are
  Internet Archive searches (free, counted by cost monitoring)
  and the optional YouTube provider (disabled without a key).
  Nothing was added that calls a model.
