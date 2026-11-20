"""Generate known test footage for the Video Intelligence Agent (Prompt 05, Phase 7).

Writes two small MP4s into data/known_footage/ plus a manifest JSON
with the ground-truth scene boundaries — the timestamps are known
because this script chose them when it drew the frames, so they are
manually verified by construction, not inferred afterwards.

* known_scenes.mp4 (30 s, 320x240, 30 fps):
    0–5 s   dark blue, static            (low motion)
    5–11 s  green, fast moving square    (high motion)
    11–20 s red, slow drifting square   (moderate motion)
    20–24 s yellow, fast moving square  (high motion)
    24–30 s grey, static                (low motion)
  Ground-truth scene boundaries: 5, 11, 20, 24 s.
* known_black.mp4 (12 s): 0–4 s black, then 4–12 s green with a
  moving square. The black segment is unusable and must be dropped.

Usage:
    .venv/bin/python scripts/make_known_footage.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "known_footage"

# (start, end, BGR colour, speed px/frame, square size)
# The square is black (uniform contrast on every background) and
# ping-pongs at the frame edges instead of wrapping, so the motion
# measured between 1 fps samples is proportional to speed and does
# not alias: fast segments read clearly above the 6.0 motion
# threshold, the slow one below it, static reads 0.
SCENES = [
    (0, 5, (120, 40, 20), 0, 0),      # dark blue, static
    (5, 11, (40, 160, 40), 7, 50),    # green, fast square
    (11, 20, (40, 40, 180), 1, 50),   # red, slow square
    (20, 24, (40, 200, 220), 7, 50),  # yellow, fast square
    (24, 30, (140, 140, 140), 0, 0),  # grey, static
]
FPS, W, H = 30, 320, 240


def _pingpong(frame_index: int, speed: int, span: int) -> int:
    """Bounce position: 0..span..0, so motion never aliases with sampling."""
    p = (frame_index * speed) % (2 * span)
    return p if p <= span else 2 * span - p


def _write(path: Path, segments: list[tuple[float, float, tuple, int, int]], duration: float) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    total = int(duration * FPS)
    for i in range(total):
        t = i / FPS
        seg = next(s for s in segments if s[0] <= t < s[1] or (s[1] == duration and t >= s[0]))
        _, _, colour, speed, size = seg
        frame = np.full((H, W, 3), colour, dtype=np.uint8)
        if size:
            x = _pingpong(i, speed, W - size)
            y = H // 2 - size // 2
            cv2.rectangle(frame, (x, y), (x + size, y + size), (0, 0, 0), -1)
        writer.write(frame)
    writer.release()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / "known_scenes.mp4", SCENES, 30)
    black_segments = [(0, 4, (0, 0, 0), 0, 0), (4, 12, (40, 160, 40), 7, 50)]
    _write(OUT / "known_black.mp4", black_segments, 12)
    manifest = {
        "known_scenes.mp4": {
            "duration_seconds": 30,
            "scene_boundaries_seconds": [5, 11, 20, 24],
            "segments": [
                {"start": s, "end": e, "motion": "static" if sp == 0 else "fast" if sp >= 6 else "slow"}
                for s, e, _, sp, _ in SCENES],
            "expected_high_motion_moments": [[5, 11], [20, 24]],
            "note": "Boundaries are ground truth: chosen when the frames were drawn.",
        },
        "known_black.mp4": {
            "duration_seconds": 12,
            "scene_boundaries_seconds": [4],
            "segments": [{"start": 0, "end": 4, "motion": "none (black)"},
                         {"start": 4, "end": 12, "motion": "fast"}],
            "expected_usable_moments": [[4, 12]],
            "note": "The 0–4 s black segment must be dropped as unusable.",
        },
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {OUT / 'known_scenes.mp4'} and {OUT / 'known_black.mp4'}")
    print(f"manifest: {OUT / 'manifest.json'}")


if __name__ == "__main__":
    main()
