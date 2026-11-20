"""RANKFLOW AI — Video Production Agent, Phase 7: audio design (Prompt 08).

Audio assets in this project are:
* the source clip audio (kept, at its original level — it is the
  footage's own sound, already rights-approved with the clip);
* narration audio — NOT generated (see narration.py);
* licensed background music — only when PROD_MUSIC_DIR points at
  a directory of licensed audio files that probe as audio;
* licensed sound effects — only when PROD_SFX_DIR points at a
  directory of licensed audio files.

No licensed music/SFX library is configured in this environment,
so the default render has no music and no SFX, and the manifest
records ``music: not_included_no_licensed_library`` and
``sfx: not_included_no_licensed_library``. No tone, no royalty-free
guess and no downloaded track is ever substituted — a silent
background is honest, a fake "licensed" track is not.

When assets ARE configured, they are picked deterministically
(first file, sorted by name, that probes as audio) and recorded
in the manifest by filename. Ducking only applies when both a
narration track and a music track exist; with neither, ducking
is recorded as not_applicable, not claimed.

The final mix is normalised with loudnorm (EBU R128, -16 LUFS),
faded in over 0.5s and out over the last 1.0s, and limited to
prevent clipping.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


class AudioAsset(BaseModel):
    kind: str  # "music" | "sfx"
    status: str  # "included" | "not_included_no_licensed_library" | "not_included_no_matching_file"
    file_path: str | None = None
    file_name: str | None = None
    duration_seconds: float | None = None
    detail: str = ""


def _probe_audio_duration(path: Path, *, ffprobe_path: str = "ffprobe") -> float | None:
    try:
        proc = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "format=duration",
             "-show_entries", "stream=codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        if not (data.get("streams") or []):
            return None
        duration = float((data.get("format") or {}).get("duration") or 0)
        return duration if duration > 0 else None
    except Exception:
        return None


def find_audio_asset(directory: str | Path, kind: str, *,
                     name_contains: str | None = None,
                     ffprobe_path: str = "ffprobe") -> AudioAsset:
    """Find one licensed audio asset in a configured directory.

    Deterministic: the first file (sorted by name) with an audio
    extension that probes as audio. Files that do not probe as
    audio are skipped, never used.
    """
    if not directory:
        return AudioAsset(kind=kind, status="not_included_no_licensed_library",
                          detail="no licensed audio directory is configured "
                                 f"for {kind} (PROD_{kind.upper()}_DIR is empty)")
    root = Path(directory)
    if not root.is_dir():
        return AudioAsset(kind=kind, status="not_included_no_licensed_library",
                          detail=f"configured {kind} directory does not exist: {root}")
    candidates = sorted(p for p in root.iterdir()
                        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
                        and (name_contains is None or name_contains in p.name.lower()))
    for candidate in candidates:
        duration = _probe_audio_duration(candidate, ffprobe_path=ffprobe_path)
        if duration is None:
            continue
        return AudioAsset(kind=kind, status="included", file_path=str(candidate),
                          file_name=candidate.name, duration_seconds=duration,
                          detail=f"licensed {kind} file from the configured directory")
    return AudioAsset(kind=kind, status="not_included_no_matching_file",
                      detail=f"no file in {root} probed as audio"
                             + (f" matching {name_contains!r}" if name_contains else ""))


def find_music(settings, *, ffprobe_path: str = "ffprobe") -> AudioAsset:
    return find_audio_asset(settings.prod_music_dir, "music", ffprobe_path=ffprobe_path)


def find_sfx(settings, *, name_contains: str, ffprobe_path: str = "ffprobe") -> AudioAsset:
    return find_audio_asset(settings.prod_sfx_dir, "sfx", name_contains=name_contains,
                            ffprobe_path=ffprobe_path)


def final_audio_filter(total_duration_seconds: float) -> str:
    """Normalisation + fades + limiter for the final mix."""
    fade_out_start = max(0.0, total_duration_seconds - 1.0)
    return (f"loudnorm=I=-16:TP=-1.5:LRA=11,"
            f"afade=t=in:st=0:d=0.5,"
            f"afade=t=out:st={fade_out_start:.2f}:d=1.0,"
            f"alimiter=limit=0.95")


def ducking_status(*, narration_generated: bool, music_included: bool) -> str:
    """Whether narration ducking applies to this render."""
    if narration_generated and music_included:
        return "applicable"
    return "not_applicable"
