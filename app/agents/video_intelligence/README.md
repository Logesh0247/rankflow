# Video Intelligence — BUILT (Prompt 05)

Video Intelligence Agent (Prompt 05) — video understanding, event detection, frame analysis, timestamp extraction, moment identification.

**What it does:** for each Rights-approved candidate with a local footage file, it probes the file with ffprobe (duration, resolution, frame rate, codecs, audio), samples frames with OpenCV, segments the video at measured scene changes, detects candidate moments, re-verifies every timestamp against the original file (with lead-in and aftermath), and persists `AnalyzedMoment` rows with thumbnails. See `docs/VIDEO_INTELLIGENCE.md`.

**Honest limit:** the analyser is `heuristic_frame_signals_v1` — deterministic frame signals only (scene changes, motion, brightness, sharpness). No vision-language model is configured in this environment, so no semantic event is inferred; moment descriptions are signal-based by design. Unapproved footage is never analysed (the Rights gate runs first, server-side). Downloading source videos is not configured; footage must be supplied via `ApprovedFootage.storage_path` or the `footage_paths` task input.
