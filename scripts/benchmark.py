"""RANKFLOW AI — Prompt 12 performance benchmark (before/after measurement).

Measures the two CPU-hot paths with real files and real wall-clock time:
1. Video Intelligence frame extraction on a 30 s clip and a 120 s clip.
2. Production concat: full re-encode (current) vs video stream-copy
   with audio re-encode (candidate optimisation).

Usage:
    .venv/bin/python scripts/benchmark.py [--concat-only] [--frames-only]

Writes results to stdout as JSON. No production data is touched;
all outputs go to a temp directory.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def _time(fn):
    started = time.monotonic()
    result = fn()
    return result, round(time.monotonic() - started, 3)


def _make_long_clip(out: Path, seconds: int = 120) -> Path:
    """A 120 s 640x360 test clip (testsrc, keyframe every 60 frames)."""
    if out.exists():
        return out
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc=duration={seconds}:size=640x360:rate=30",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
         "-g", "60", str(out)],
        check=True)
    return out


def benchmark_frames(tmp: Path) -> dict:
    from app.agents.video_intelligence.frames import extract_samples

    results: dict = {}
    for name, path in [
        ("known_30s", ROOT / "data" / "known_footage" / "known_scenes.mp4"),
        ("synthetic_120s", _make_long_clip(tmp / "long120.mp4")),
    ]:
        extraction, seconds = _time(lambda p=path: extract_samples(p))
        results[name] = {
            "seconds": seconds,
            "samples": len(extraction.samples),
            "effective_fps": extraction.effective_fps,
            "scene_boundaries": extraction.scene_boundaries,
        }
    return results


def _make_segments(tmp: Path) -> list[Path]:
    from app.agents.production.extract import extract_clip, render_silent_segment

    src = ROOT / "data" / "known_footage" / "known_scenes.mp4"
    seg1 = tmp / "seg1.mp4"
    seg2 = tmp / "seg2.mp4"
    card = tmp / "card.mp4"
    extract_clip(src, 5.0, 11.0, seg1)
    extract_clip(src, 20.0, 24.0, seg2)
    render_silent_segment(card, 3.0)
    return [card, seg1, seg2]


def benchmark_concat(tmp: Path) -> dict:
    from app.agents.production.audio import final_audio_filter
    from app.agents.production.render import (
        _concat_reencode, concat_segments, validate_render,
    )

    segments = _make_segments(tmp)
    total = 3.0 + 6.0 + 4.0
    out_reencode = tmp / "final-reencode.mp4"
    out_copy = tmp / "final-copy.mp4"
    # True re-encode baseline: the Prompt 08 fallback path, not the
    # (now optimised) concat_segments entry point.
    list_path = tmp / "bench.concat.txt"
    from app.agents.production.render import _escape_concat_path
    list_path.write_text(
        "".join(f"file '{_escape_concat_path(str(s))}'\n" for s in segments),
        encoding="utf-8")
    audio_chain = final_audio_filter(total)
    _, t_reencode = _time(lambda: _concat_reencode(
        list_path, out_reencode, None, audio_chain,
        ffmpeg_path="ffmpeg", timeout_seconds=600.0))
    _, t_copy = _time(lambda: concat_segments(
        segments, out_copy, total_duration_seconds=total))
    val_reencode = validate_render(out_reencode, expected_duration_seconds=total)
    val_copy = validate_render(out_copy, expected_duration_seconds=total)
    return {
        "reencode_seconds": t_reencode,
        "stream_copy_seconds": t_copy,
        "speedup": round(t_reencode / t_copy, 2) if t_copy else None,
        "reencode_valid": val_reencode.valid,
        "stream_copy_valid": val_copy.valid,
        "stream_copy_issues": val_copy.issues,
        "reencode_bytes": out_reencode.stat().st_size,
        "stream_copy_bytes": out_copy.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames-only", action="store_true")
    parser.add_argument("--concat-only", action="store_true")
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="rankflow-bench-"))
    results: dict = {"tmp": str(tmp)}
    if not args.concat_only:
        results["frames"] = benchmark_frames(tmp)
    if not args.frames_only:
        results["concat"] = benchmark_concat(tmp)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
