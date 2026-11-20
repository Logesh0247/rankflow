"""RANKFLOW AI — Video Production Agent, Phase 4: narration (Prompt 08).

Narration audio is NOT generated in this environment, and this
module is where that is decided — honestly, not silently.

A TTS provider is only usable when one is configured
(TTS_PROVIDER + TTS_API_KEY) AND an implementation for it exists,
or when a local TTS binary (espeak-ng, flite, piper) is installed.
In this environment neither is true: TTS_PROVIDER is empty by
default, no TTS API key is configured, and no local TTS binary is
installed (checked at runtime with shutil.which). So
``NarrationProvider.status`` is ``not_configured`` and
``generate`` raises ExternalServiceError naming exactly what is
missing. Production catches that, renders the video with a
silent audio track instead, and records
``narration_audio: not_generated_no_tts_provider`` in the render
manifest — the narration SCRIPT still exists (Creative Director),
the captions still show it, but no voice is claimed.

If a TTS provider is configured later, this module is the one
place to add its implementation; the service and manifest already
carry the provider/status fields for it.
"""
from __future__ import annotations

import shutil

from pydantic import BaseModel

from app.agents.creative_director.narration import estimate_seconds
from app.core.config import Settings
from app.core.errors import ExternalServiceError

LOCAL_TTS_BINARIES = ("espeak-ng", "espeak", "flite", "piper")


class NarrationProvider(BaseModel):
    """What narration audio can (and cannot) be produced right now."""

    provider: str = "none"
    status: str = "not_configured"  # not_configured | local_binary | configured_unsupported
    local_binary: str | None = None
    detail: str = ""


def detect_narration_provider(settings: Settings) -> NarrationProvider:
    """Detect a usable TTS route. Never claims one that is not there."""
    for binary in LOCAL_TTS_BINARIES:
        if shutil.which(binary):
            return NarrationProvider(
                provider=f"local:{binary}", status="local_binary",
                local_binary=binary,
                detail=f"local TTS binary {binary} is installed")
    if settings.tts_provider and settings.tts_api_key:
        return NarrationProvider(
            provider=settings.tts_provider, status="configured_unsupported",
            detail=f"TTS provider {settings.tts_provider!r} is configured but "
                   "no implementation for it exists in this project — "
                   "narration audio is not generated")
    if settings.tts_provider and not settings.tts_api_key:
        return NarrationProvider(
            provider=settings.tts_provider, status="not_configured",
            detail=f"TTS provider {settings.tts_provider!r} is named but no "
                   "TTS_API_KEY is configured")
    return NarrationProvider(
        provider="none", status="not_configured",
        detail="no TTS provider is configured (TTS_PROVIDER / TTS_API_KEY "
               "are empty) and no local TTS binary is installed")


def narration_estimated_seconds(script_text: str, *, words_per_minute: float = 150.0) -> float:
    """Estimated spoken length of the narration script (150 wpm)."""
    return estimate_seconds(script_text, words_per_minute=words_per_minute)


def generate_narration_audio(script_text: str, output_path: str,
                             settings: Settings) -> str:
    """Generate narration audio for a script — or fail honestly.

    There is no TTS implementation in this project yet, so this
    always raises ExternalServiceError describing what is missing.
    It never writes a fake audio file, a tone, or silence and
    calls it narration.
    """
    provider = detect_narration_provider(settings)
    raise ExternalServiceError(
        f"narration audio cannot be generated: {provider.detail} "
        "(the narration script exists and the captions carry it; "
        "the render uses a silent audio track instead)")
