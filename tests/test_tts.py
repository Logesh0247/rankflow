"""Edge TTS narration tests: config, provider detection, chunking,
synthesis (with a stubbed ``edge_tts.Communicate`` — live synthesis
is UNVERIFIED on this machine because the network proxy blocks
speech.platform.bing.com, so no test here ever makes a real
network call), failure fallback, and $0 cost recording.

The stub stands in for the Edge TTS endpoint: it records the text
and voice it was asked to synthesise and writes a real tone MP3
(generated with FFmpeg) so duration probing and mixing work
exactly as they would with a real voice file. Nothing here claims
a live voice was heard.
"""
import subprocess

import pytest

from app.agents.production.narration import (
    ALTERNATIVE_VOICE, DEFAULT_VOICE, EDGE_TTS_PROVIDER,
    NarrationAudio, detect_narration_provider, generate_narration_audio,
    split_script_into_chunks,
)
from app.core.config import Settings
from app.core.cost import CostService
from app.core.errors import ExternalServiceError


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 30.0,
            "rights_fetch_ia_metadata": False}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(scope="module")
def tone_mp3(tmp_path_factory):
    """A real 3-second tone MP3 the stub synthesiser copies out."""
    path = tmp_path_factory.mktemp("tts-fixture") / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:a", "libmp3lame",
                    "-q:a", "4", str(path)], check=True)
    return path


class FakeCommunicate:
    """Stand-in for edge_tts.Communicate: records calls, writes tone audio."""

    calls: list = []

    def __init__(self, text, voice="en-US-EmmaMultilingualNeural", **kwargs):
        self.text = text
        self.voice = voice
        FakeCommunicate.calls.append((text, voice))

    def save_sync(self, output_path):
        import shutil
        shutil.copyfile(FakeCommunicate.tone_path, output_path)


class FailingCommunicate:
    """Stand-in that fails like the blocked live endpoint does."""

    def __init__(self, *args, **kwargs):
        pass

    def save_sync(self, output_path):
        raise RuntimeError("Cannot connect to host speech.platform.bing.com:443 "
                           "ssl: [SSL: WRONG_VERSION_NUMBER] wrong version number")


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch, tone_mp3):
    FakeCommunicate.calls = []
    FakeCommunicate.tone_path = tone_mp3
    yield
    FakeCommunicate.calls = []


def _patch_communicate(monkeypatch, cls):
    import edge_tts
    monkeypatch.setattr(edge_tts, "Communicate", cls)


# ---------------------------------------------------------------- config + detection
def test_config_defaults_are_edge_tts_free_no_key():
    settings = Settings()
    assert settings.tts_provider == EDGE_TTS_PROVIDER
    assert settings.tts_voice == DEFAULT_VOICE
    assert settings.tts_enabled is True
    assert settings.tts_api_key == ""  # edge-tts needs no key; none is invented


def test_detect_provider_is_edge_tts_by_default():
    provider = detect_narration_provider(_settings())
    assert provider.provider == EDGE_TTS_PROVIDER
    assert provider.status == "edge_tts"
    assert provider.voice == DEFAULT_VOICE
    assert "no API key" in provider.detail


def test_detect_provider_disabled_is_not_configured():
    provider = detect_narration_provider(_settings(tts_enabled=False))
    assert provider.provider == "none"
    assert provider.status == "not_configured"
    assert "TTS_ENABLED=false" in provider.detail


def test_detect_provider_unsupported_name_is_honest():
    provider = detect_narration_provider(_settings(tts_provider="other-tts"))
    assert provider.provider == "other-tts"
    assert provider.status == "configured_unsupported"
    assert "no implementation" in provider.detail


def test_detect_provider_alternative_voice_is_accepted():
    provider = detect_narration_provider(_settings(tts_voice=ALTERNATIVE_VOICE))
    assert provider.status == "edge_tts"
    assert provider.voice == ALTERNATIVE_VOICE


# ---------------------------------------------------------------- chunking
def test_split_script_empty_and_short():
    assert split_script_into_chunks("") == []
    assert split_script_into_chunks("   ") == []
    assert split_script_into_chunks("Number five. A sharp segment.") == [
        "Number five. A sharp segment."]


def test_split_script_long_text_chunks_and_roundtrips():
    script = " ".join(f"Sentence number {i} with some words in it." for i in range(60))
    chunks = split_script_into_chunks(script)
    assert len(chunks) > 1
    assert all(len(c) <= 900 for c in chunks)
    assert " ".join(chunks) == script


def test_split_script_never_splits_mid_word():
    script = "Word " * 300 + "end."
    chunks = split_script_into_chunks(script.strip())
    assert all(len(c) <= 900 for c in chunks)
    assert " ".join(chunks) == script.strip()


# ---------------------------------------------------------------- synthesis (stubbed)
def test_generate_with_stubbed_edge_tts(monkeypatch, tmp_path):
    _patch_communicate(monkeypatch, FakeCommunicate)
    out = tmp_path / "narration.mp3"
    audio = generate_narration_audio(
        "Number five. A sharp segment. Number four. A steady segment.",
        str(out), _settings())
    assert isinstance(audio, NarrationAudio)
    assert audio.status == "generated"
    assert audio.provider == EDGE_TTS_PROVIDER
    assert audio.voice == DEFAULT_VOICE
    assert audio.file_path == str(out)
    assert out.is_file() and out.stat().st_size > 0
    assert audio.duration_seconds == pytest.approx(3.0, abs=0.3)
    assert audio.chunks == 1
    assert audio.chars == len(
        "Number five. A sharp segment. Number four. A steady segment.")
    assert FakeCommunicate.calls[0][1] == DEFAULT_VOICE


def test_generate_uses_configured_voice(monkeypatch, tmp_path):
    _patch_communicate(monkeypatch, FakeCommunicate)
    out = tmp_path / "narration.mp3"
    audio = generate_narration_audio("Number five.", str(out),
                                     _settings(tts_voice=ALTERNATIVE_VOICE))
    assert audio.voice == ALTERNATIVE_VOICE
    assert FakeCommunicate.calls[0][1] == ALTERNATIVE_VOICE


def test_generate_long_script_synthesises_every_chunk(monkeypatch, tmp_path):
    _patch_communicate(monkeypatch, FakeCommunicate)
    script = " ".join(f"Sentence number {i} with some words in it." for i in range(60))
    out = tmp_path / "narration.mp3"
    audio = generate_narration_audio(script, str(out), _settings())
    assert audio.status == "generated"
    assert audio.chunks == len(split_script_into_chunks(script)) > 1
    assert len(FakeCommunicate.calls) == audio.chunks
    assert " ".join(text for text, _ in FakeCommunicate.calls) == script
    # Chunks joined: one 3s tone per chunk, concatenated.
    assert audio.duration_seconds == pytest.approx(3.0 * audio.chunks, abs=0.5)


def test_generate_failure_raises_honestly_and_writes_nothing(monkeypatch, tmp_path):
    _patch_communicate(monkeypatch, FailingCommunicate)
    out = tmp_path / "narration.mp3"
    with pytest.raises(ExternalServiceError, match="narration audio cannot be generated"):
        generate_narration_audio("Number five. A sharp segment.", str(out), _settings())
    assert not out.is_file()  # never a fake file passed off as narration


def test_generate_disabled_never_calls_the_endpoint(monkeypatch, tmp_path):
    _patch_communicate(monkeypatch, FakeCommunicate)
    out = tmp_path / "narration.mp3"
    with pytest.raises(ExternalServiceError, match="narration audio cannot be generated"):
        generate_narration_audio("Number five.", str(out),
                                 _settings(tts_enabled=False))
    assert FakeCommunicate.calls == []
    assert not out.is_file()


def test_generate_empty_script_raises(monkeypatch, tmp_path):
    _patch_communicate(monkeypatch, FakeCommunicate)
    with pytest.raises(ExternalServiceError, match="empty"):
        generate_narration_audio("   ", str(tmp_path / "narration.mp3"), _settings())
    assert FakeCommunicate.calls == []


# ---------------------------------------------------------------- cost: $0 because free
def test_tts_cost_event_is_zero_because_free(engine):
    from app.orchestrator.jobs import JobService
    from app.schemas.schemas import JobCreate

    job = JobService(engine).create_job(JobCreate(topic="TOP 5 CRAZIEST PARKOUR FAILS"))
    CostService(engine, _settings()).record_tts(job.id, chars=138)
    summary = CostService(engine, _settings()).job_cost(job.id)
    assert summary.tts_chars == 138
    assert summary.tts_usd == 0.0
    assert any("free" in note for note in summary.notes)
