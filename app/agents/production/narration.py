"""RANKFLOW AI — Video Production Agent, Phase 4: narration (Prompt 08,
Edge TTS synthesis added after Prompt 12).

Narration audio is synthesised with Edge TTS (``edge-tts`` package),
a free, unofficial client of Microsoft's Edge neural-voice
endpoint: no API key, no account, no per-character charge.
Default voice is ``en-US-GuyNeural`` (``en-US-JennyNeural`` is the
configured alternative); the voice is configurable via
``TTS_VOICE`` and synthesis can be switched off with
``TTS_ENABLED=false``.

How it works
------------
The Creative Director narration script is a continuous voiceover
for the whole timeline (intro + five moments + outro), so it is
synthesised as one MP3 starting at t=0 and mixed under the full
render in ``render.py`` (narration is the primary track; music,
when present, stays at low volume). The script is split into
sentence chunks of at most ``MAX_CHUNK_CHARS`` characters and each
chunk is synthesised separately, then the chunks are joined with
FFmpeg — a single very long request is never sent to the endpoint.

Failure is FAIL-SOFT, by design: ``generate_narration_audio``
raises ``ExternalServiceError`` on any synthesis failure and the
production service catches it, renders with a silent narration
track, and records the failure reason in the manifest
(status ``generation_failed``). A TTS endpoint failure must never
hard-fail a whole job. QA then reports ``needs_review`` (no voice,
silent audio) instead of ``pass``.

LIVE-SYNTHESIS LIMITATION — READ THIS
--------------------------------------
Live synthesis is UNVERIFIED on this machine: this VM's network
proxy blocks ``speech.platform.bing.com:443`` (``SSL
WRONG_VERSION_NUMBER`` — measured 2026-11-21, fails in ~1s), so
every real synthesis attempt here fails. That failure proves
nothing about the deploy host. Live verification must happen on
Render once the deploy there is fixed. Tests therefore use a
stubbed ``edge_tts.Communicate`` and never claim a live voice
was heard. The code never claims a generated file is a real
voice when it was not synthesised — a stub is only ever used
inside tests.

Commercial-use caveat: edge-tts is unofficial, has no SLA, and
the endpoint may rate-limit; check Microsoft's commercial-use
terms before publishing narrated videos. If Edge TTS proves
unreliable, the documented fallback is Piper (fully offline,
needs a voice model downloaded, less natural voice).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.creative_director.narration import estimate_seconds
from app.agents.production.audio import _probe_audio_duration
from app.core.config import Settings
from app.core.errors import ExternalServiceError

LOCAL_TTS_BINARIES = ("espeak-ng", "espeak", "flite", "piper")

# Edge TTS provider name and voices.
EDGE_TTS_PROVIDER = "edge-tts"
DEFAULT_VOICE = "en-US-GuyNeural"
ALTERNATIVE_VOICE = "en-US-JennyNeural"

# A chunk is one synthesis request; keep it well under the
# endpoint's per-request limits and sentence-shaped.
MAX_CHUNK_CHARS = 900

# Statuses: "edge_tts" (implemented) | "local_binary" (detected,
# no implementation) | "configured_unsupported" | "not_configured".


class NarrationProvider(BaseModel):
    """What narration audio can (and cannot) be produced right now."""

    provider: str = "none"
    status: str = "not_configured"  # not_configured | edge_tts | local_binary | configured_unsupported
    local_binary: str | None = None
    voice: str | None = None
    detail: str = ""


class NarrationAudio(BaseModel):
    """A synthesised narration track — only ever built from a real
    synthesis run (or a test stub standing in for one)."""

    provider: str = EDGE_TTS_PROVIDER
    voice: str = DEFAULT_VOICE
    status: str = "generated"  # "generated" only; failures raise instead
    file_path: str
    duration_seconds: float
    chars: int = 0
    chunks: int = 0
    detail: str = ""


def detect_narration_provider(settings: Settings) -> NarrationProvider:
    """Detect a usable TTS route. Never claims one that is not there."""
    if not settings.tts_enabled:
        return NarrationProvider(
            provider="none", status="not_configured",
            detail="TTS is disabled (TTS_ENABLED=false) — narration "
                   "audio is not generated")
    if settings.tts_provider == EDGE_TTS_PROVIDER:
        return NarrationProvider(
            provider=EDGE_TTS_PROVIDER, status="edge_tts",
            voice=settings.tts_voice or DEFAULT_VOICE,
            detail=f"Edge TTS (free, no API key) with voice "
                   f"{settings.tts_voice or DEFAULT_VOICE!r} — live "
                   "synthesis is unverified on this machine "
                   "(speech.platform.bing.com is blocked by the network "
                   "proxy); verify on the deploy host")
    for binary in LOCAL_TTS_BINARIES:
        if shutil.which(binary):
            return NarrationProvider(
                provider=f"local:{binary}", status="local_binary",
                local_binary=binary,
                detail=f"local TTS binary {binary} is installed but no "
                       "implementation for it exists — narration audio "
                       "is not generated")
    if settings.tts_provider:
        return NarrationProvider(
            provider=settings.tts_provider, status="configured_unsupported",
            detail=f"TTS provider {settings.tts_provider!r} is configured but "
                   "no implementation for it exists in this project — "
                   "narration audio is not generated")
    return NarrationProvider(
        provider="none", status="not_configured",
        detail="no TTS provider is configured (TTS_PROVIDER is empty) "
               "and no local TTS binary is installed")


def narration_estimated_seconds(script_text: str, *, words_per_minute: float = 150.0) -> float:
    """Estimated spoken length of the narration script (150 wpm)."""
    return estimate_seconds(script_text, words_per_minute=words_per_minute)


def split_script_into_chunks(script_text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a narration script into sentence-shaped chunks.

    Sentences are never split mid-word; a single sentence longer
    than ``max_chars`` is cut at a word boundary. Empty input
    returns no chunks.
    """
    text = " ".join((script_text or "").split())
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            words = sentence.split(" ")
            piece = ""
            for word in words:
                candidate = f"{piece} {word}".strip() if piece else word
                if len(candidate) > max_chars and piece:
                    chunks.append(piece)
                    piece = word
                else:
                    piece = candidate
            if piece:
                chunks.append(piece)
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _synthesize_chunk(chunk_text: str, voice: str, output_path: Path,
                      *, timeout_seconds: float) -> None:
    """One Edge TTS request, synchronously. Raises on any failure."""
    import edge_tts

    communicate = edge_tts.Communicate(
        chunk_text, voice,
        connect_timeout=min(10, max(1, int(timeout_seconds))),
        receive_timeout=max(1, int(timeout_seconds)))
    communicate.save_sync(str(output_path))


def _escape_concat_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace("'", "'\\''")


def _concat_mp3_chunks(chunk_paths: list[Path], output_path: Path, *,
                       ffmpeg_path: str = "ffmpeg") -> None:
    """Join chunk MP3s into one MP3 with FFmpeg (re-encode)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = output_path.with_suffix(".chunks.txt")
    list_path.write_text(
        "".join(f"file '{_escape_concat_path(str(p))}'\n" for p in chunk_paths),
        encoding="utf-8")
    proc = subprocess.run(
        [ffmpeg_path, "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(list_path), "-c:a", "libmp3lame", "-q:a", "4", str(output_path)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        raise ExternalServiceError(
            "narration audio cannot be generated: joining the synthesised "
            f"chunks failed: {(proc.stderr or '').strip() or 'ffmpeg concat failed'}")


def generate_narration_audio(script_text: str, output_path: str | Path,
                             settings: Settings, *,
                             voice: str | None = None,
                             ffmpeg_path: str = "ffmpeg") -> NarrationAudio:
    """Synthesise the narration script to one MP3 with Edge TTS.

    Returns a ``NarrationAudio`` with the voice, file and measured
    duration. Raises ``ExternalServiceError`` on any failure — it
    never writes a fake audio file, a tone, or silence and calls it
    narration. The caller (production service) catches the failure,
    falls back to a silent narration track and records the reason.
    """
    provider = detect_narration_provider(settings)
    if provider.status != "edge_tts":
        raise ExternalServiceError(
            f"narration audio cannot be generated: {provider.detail} "
            "(the narration script exists and the captions carry it; "
            "the render uses a silent audio track instead)")
    voice = voice or provider.voice or DEFAULT_VOICE
    chunks = split_script_into_chunks(script_text)
    if not chunks:
        raise ExternalServiceError(
            "narration audio cannot be generated: the narration script "
            "is empty — nothing to synthesise")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    timeout = float(settings.tts_timeout_seconds)
    chunk_paths: list[Path] = []
    try:
        for index, chunk_text in enumerate(chunks):
            chunk_path = output.with_name(f"{output.stem}.chunk-{index:02d}.mp3")
            try:
                _synthesize_chunk(chunk_text, voice, chunk_path,
                                  timeout_seconds=timeout)
            except Exception as exc:  # noqa: BLE001 — any synthesis failure is fail-soft upstream
                raise ExternalServiceError(
                    "narration audio cannot be generated: Edge TTS "
                    f"synthesis failed on chunk {index + 1}/{len(chunks)} "
                    f"(voice {voice!r}): {exc} "
                    "(live synthesis is unverified on this machine — "
                    "speech.platform.bing.com is blocked by the network "
                    "proxy; verify on the deploy host)") from exc
            if not chunk_path.is_file() or chunk_path.stat().st_size == 0:
                raise ExternalServiceError(
                    "narration audio cannot be generated: Edge TTS "
                    f"returned an empty chunk {index + 1}/{len(chunks)} "
                    f"(voice {voice!r})")
            chunk_paths.append(chunk_path)
        if len(chunk_paths) == 1:
            shutil.copyfile(chunk_paths[0], output)
        else:
            _concat_mp3_chunks(chunk_paths, output, ffmpeg_path=ffmpeg_path)
    finally:
        for chunk_path in chunk_paths:
            try:
                chunk_path.unlink(missing_ok=True)
            except OSError:
                pass
        list_path = output.with_suffix(".chunks.txt")
        try:
            list_path.unlink(missing_ok=True)
        except OSError:
            pass
    duration = _probe_audio_duration(output, ffprobe_path="ffprobe")
    if duration is None:
        raise ExternalServiceError(
            "narration audio cannot be generated: the synthesised MP3 "
            "did not probe as audio — refusing to mix an unverified file")
    return NarrationAudio(
        provider=EDGE_TTS_PROVIDER, voice=voice, status="generated",
        file_path=str(output), duration_seconds=round(duration, 2),
        chars=len(script_text), chunks=len(chunks),
        detail=f"Edge TTS synthesis, voice {voice!r}, {len(chunks)} chunk(s), "
               f"{duration:.1f}s of audio (free, no API key)")
