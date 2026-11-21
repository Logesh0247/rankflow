"""YouTube search + download tests (post-Prompt 12 wiring).

YouTube is Creative Commons only: search is filtered to
videoLicense=creativeCommon, each video's status.license is
fetched, CC BY 3.0 videos are recorded with the CC BY 3.0
licence URL (Rights approves them), and standard-licence videos
are recorded as such (Rights rejects them, and the downloader
never touches them). Research can search YouTube and the
Internet Archive together (RESEARCH_PROVIDERS), and approved
CC YouTube videos are downloaded with yt-dlp under the same
size caps as Archive footage.

No test touches the live YouTube API: googleapis.com and
youtube.com are unreachable from this VM (the egress proxy
blocks them), so the Data API is stubbed with MockTransport
and yt-dlp is stubbed with a fake runner that copies a real
OpenCV-generated clip into place. Live verification must happen
on the deploy host, via a Render-side job.
"""
import subprocess
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.research.providers import (
    YOUTUBE_CC_LICENCE_URL,
    ProviderUnavailable,
    RawCandidate,
    YouTubeProvider,
)
from app.agents.research.scoring import ScoredCandidate
from app.agents.research.service import ResearchService
from app.agents.rights.evidence import evaluate
from app.agents.rights.service import RightsService
from app.agents.video_intelligence.download import FootageDownloader
from app.agents.video_intelligence.service import VideoIntelligenceService
from app.api.system_status import _effective_providers
from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.models.models import (
    ApprovedFootage,
    CandidateVideo,
    RightsEvidence,
    Source,
)
from app.orchestrator.jobs import JobService
from app.schemas.schemas import JobCreate

FPS, W, H = 15, 160, 120
SCENES = [
    (0, 5, (120, 40, 20), 0, 0),
    (5, 11, (40, 160, 40), 7, 50),
    (11, 20, (40, 40, 180), 1, 50),
    (20, 24, (40, 200, 220), 7, 50),
    (24, 30, (140, 140, 140), 0, 0),
]


def _pingpong(frame_index: int, speed: int, span: int) -> int:
    p = (frame_index * speed) % (2 * span)
    return p if p <= span else 2 * span - p


def _write_footage(path: Path, duration: float = 30.0) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for i in range(int(duration * FPS)):
        t = i / FPS
        seg = next(s for s in SCENES if s[0] <= t < s[1] or (s[1] == duration and t >= s[0]))
        _, _, colour, speed, size = seg
        frame = np.full((H, W, 3), colour, dtype=np.uint8)
        if size:
            x = _pingpong(i, speed, W - size)
            cv2.rectangle(frame, (x, H // 2 - size // 2), (x + size, H // 2 + size // 2),
                          (0, 0, 0), -1)
        writer.write(frame)
    writer.release()
    return path


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 300.0,
            "rights_fetch_ia_metadata": False,
            "footage_download_enabled": True}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic="TOP 5 FUNNIEST CAT VIDEOS"):
    return JobService(engine).create_job(JobCreate(topic=topic))


def _seed_candidate(engine, job_id, external_id, *, licence=None,
                    platform="youtube", title="Funny Cat Fails",
                    creator="CatChannel", url=None):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        source = (session.query(Source)
                  .filter(Source.platform == platform,
                          Source.external_id == external_id).first())
        if source is None:
            source = Source(platform=platform, external_id=external_id,
                            title=title,
                            url=url or f"https://www.youtube.com/watch?v={external_id}")
            session.add(source)
            session.flush()
        candidate = CandidateVideo(
            job_id=job_id, source_id=source.id, external_video_id=external_id,
            title=title,
            url=url or f"https://www.youtube.com/watch?v={external_id}",
            duration_seconds=30.0, creator=creator, licence=licence)
        session.add(candidate)
        session.commit()
        return candidate.id


def _cc_url(variant: str = "by", version: str = "3.0") -> str:
    return f"https://creativecommons.org/licenses/{variant}/{version}/"


def _approve(engine, job_id, candidate_id):
    """Approve via the real Rights service (CC licence)."""
    RightsService(engine, _settings()).run(job_id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage is not None, "rights did not approve the candidate"


# ------------------------------------------------------------------ provider, stubbed API
def _youtube_handler(*, licences=None, search_status=200, videos_status=200):
    """MockTransport handler for the YouTube Data API.

    `licences` maps video id -> status.license value ("creativeCommon",
    "youtube", or None for a missing status). Records every request
    so tests can assert the CC filter was actually sent.
    """
    licences = licences or {}
    calls = {"search": [], "videos": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/youtube/v3/search"):
            calls["search"].append(dict(request.url.params))
            if search_status != 200:
                return httpx.Response(search_status, content=b"error")
            items = []
            for video_id in licences:
                items.append({"id": {"videoId": video_id},
                              "snippet": {"title": "Funny Cat Fails",
                                          "channelTitle": "CatChannel",
                                          "description": "funny cats",
                                          "publishedAt": "2024-01-01T00:00:00Z"}})
            return httpx.Response(200, json={"items": items})
        if path.endswith("/youtube/v3/videos"):
            calls["videos"].append(dict(request.url.params))
            if videos_status != 200:
                return httpx.Response(videos_status, content=b"error")
            items = []
            for video_id, licence in licences.items():
                item = {"id": video_id,
                        "contentDetails": {"duration": "PT30S"},
                        "statistics": {"viewCount": "1234"}}
                if licence is not None:
                    item["status"] = {"license": licence}
                items.append(item)
            return httpx.Response(200, json={"items": items})
        return httpx.Response(404, content=b"not found")

    handler.calls = calls
    return handler


def _youtube_provider(handler, api_key="test-key"):
    def factory(timeout):
        return httpx.Client(transport=httpx.MockTransport(handler),
                            timeout=timeout)
    return YouTubeProvider(api_key=api_key, client_factory=factory)


def test_youtube_search_sends_cc_filter_and_maps_licence():
    handler = _youtube_handler(licences={"cc-vid-1": "creativeCommon"})
    provider = _youtube_provider(handler)
    found = provider.search("funny cats", limit=5)
    assert len(found) == 1
    assert found[0].platform == "youtube"
    assert found[0].external_id == "cc-vid-1"
    assert found[0].licence == YOUTUBE_CC_LICENCE_URL
    assert found[0].licence == _cc_url("by", "3.0")
    assert found[0].duration_seconds == 30.0
    assert found[0].view_count == 1234
    # The CC filter was actually sent to the API.
    search_params = handler.calls["search"][0]
    assert search_params["videoLicense"] == "creativeCommon"
    assert search_params["type"] == "video"
    # videos.list asked for the licence status.
    assert "status" in handler.calls["videos"][0]["part"]


def test_youtube_standard_licence_video_is_marked_not_usable():
    handler = _youtube_handler(licences={"std-vid-1": "youtube"})
    found = _youtube_provider(handler).search("funny cats")
    assert len(found) == 1
    assert found[0].licence == "standard YouTube licence"
    # Rights rejects it — it can never be approved or downloaded.
    decision = evaluate(licence_raw=found[0].licence, title=found[0].title,
                        creator=found[0].creator, url=found[0].url,
                        platform="youtube")
    assert decision.verdict == "rejected"


def test_youtube_missing_licence_status_is_none():
    handler = _youtube_handler(licences={"mystery-vid": None})
    found = _youtube_provider(handler).search("funny cats")
    assert len(found) == 1
    assert found[0].licence is None
    decision = evaluate(licence_raw=None, title=found[0].title,
                        creator=found[0].creator, url=found[0].url,
                        platform="youtube")
    assert decision.verdict == "needs_review"


def test_youtube_without_key_is_unavailable_not_faked():
    with pytest.raises(ProviderUnavailable, match="YOUTUBE_API_KEY"):
        YouTubeProvider(api_key="").search("funny cats")


def test_youtube_search_http_error_raises():
    handler = _youtube_handler(licences={"cc-vid-1": "creativeCommon"},
                               search_status=403)
    with pytest.raises(httpx.HTTPStatusError):
        _youtube_provider(handler).search("funny cats")


# ------------------------------------------------------------------ rights: YouTube CC
def test_rights_approves_youtube_cc_by_3():
    decision = evaluate(licence_raw=YOUTUBE_CC_LICENCE_URL,
                        title="Funny Cat Fails", creator="CatChannel",
                        url="https://www.youtube.com/watch?v=cc-vid-1",
                        platform="youtube")
    assert decision.verdict == "approved"
    assert decision.commercial_use and decision.modification
    assert decision.redistribution and decision.attribution_required
    assert decision.licence_name == "Creative Commons Attribution 3.0"


def test_rights_rejects_youtube_standard_licence():
    decision = evaluate(licence_raw="standard YouTube licence",
                        title="Funny Cat Fails", creator="CatChannel",
                        url="https://www.youtube.com/watch?v=std-vid-1",
                        platform="youtube")
    assert decision.verdict == "rejected"
    assert "viewing only" in decision.reasons[0]


# ------------------------------------------------------------------ multi-provider research
class _StubProvider:
    """Deterministic in-memory provider for chain tests."""

    def __init__(self, name, candidates):
        self.name = name
        self._candidates = candidates

    def search(self, query, limit=25):
        return list(self._candidates)


def _raw(platform, external_id, *, title="Funny Cat Fails",
         creator="CatChannel", duration=120.0, licence=None):
    return RawCandidate(
        platform=platform, external_id=external_id, title=title,
        creator=creator, description="funny cats everywhere",
        url=(f"https://www.youtube.com/watch?v={external_id}"
             if platform == "youtube"
             else f"https://archive.org/details/{external_id}"),
        duration_seconds=duration, view_count=100,
        published_at=None, licence=licence)


def test_dedupe_keeps_distinct_youtube_videos():
    """Regression: _normalize_url used to drop the query string,
    so every youtube.com/watch?v=… URL normalised to the same key
    and dedupe kept only one YouTube candidate per job."""
    from app.agents.research.scoring import deduplicate

    keywords = ["cat", "videos"]
    items = [
        ScoredCandidate(_raw("youtube", "yt-1", licence=_cc_url()), keywords),
        ScoredCandidate(_raw("youtube", "yt-2", title="Cats Being Cats",
                             creator="OtherChannel", duration=60.0,
                             licence=_cc_url()), keywords),
        ScoredCandidate(_raw("youtube", "yt-1", licence=_cc_url()), keywords),
    ]
    kept, removed = deduplicate(items)
    assert removed == 1, "only the exact repeat is a duplicate"
    assert {i.candidate.external_id for i in kept} == {"yt-1", "yt-2"}


def test_provider_chain_multi_provider_merges_and_dedupes(engine):
    """YouTube + Archive in one run: the same video re-uploaded on
    both platforms collapses to one candidate; distinct videos stay."""
    job = _new_job(engine)
    youtube = _StubProvider("youtube", [
        _raw("youtube", "yt-1", licence=_cc_url()),
        _raw("youtube", "yt-2", title="Cats Being Cats",
             creator="OtherChannel", duration=60.0, licence=_cc_url()),
    ])
    archive = _StubProvider("internet_archive", [
        # Same video as yt-1, re-uploaded to the Archive.
        _raw("internet_archive", "ia-1", duration=125.0),
        _raw("internet_archive", "ia-2", title="Archive Cats",
             creator="Archivist", duration=90.0),
    ])
    service = ResearchService(engine, _settings())
    service._provider_chain = lambda: [youtube, archive]  # noqa: SLF001
    report = service.run(job.id)
    # Each query in the plan (exact, expanded, broad) re-asks both
    # providers, so raw counts the same records several times;
    # what matters is the kept set after scoring + dedupe.
    assert report.metrics["raw_candidates"] > 4
    assert report.metrics["duplicate_rate"] > 0
    kept_ids = {c.external_video_id for c in report.candidates}
    assert len(kept_ids) == 3, "one re-upload pair must collapse"
    assert {"yt-1", "ia-1"} & kept_ids and not ({"yt-1", "ia-1"} <= kept_ids)
    assert {"yt-2", "ia-2"} <= kept_ids
    providers = {r.provider for r in report.provider_results if r.status == "ok"}
    assert providers == {"youtube", "internet_archive"}


def test_provider_chain_research_providers_setting():
    service = ResearchService.__new__(ResearchService)
    service.settings = _settings(research_providers="youtube,internet_archive",
                                 youtube_api_key="fake-key")
    names = [p.name for p in service._provider_chain()]  # noqa: SLF001
    assert names == ["youtube", "internet_archive"]
    # Duplicates in the list are removed, order kept.
    service.settings = _settings(research_providers="internet_archive,youtube,internet_archive",
                                 youtube_api_key="fake-key")
    names = [p.name for p in service._provider_chain()]  # noqa: SLF001
    assert names == ["internet_archive", "youtube"]


def test_provider_chain_unknown_name_is_config_error():
    service = ResearchService.__new__(ResearchService)
    service.settings = _settings(research_providers="nosuch")
    with pytest.raises(ValueError, match="unknown research provider"):
        service._provider_chain()  # noqa: SLF001


def test_provider_chain_test_is_never_mixed():
    service = ResearchService.__new__(ResearchService)
    service.settings = _settings(research_providers="test,youtube")
    names = [p.name for p in service._provider_chain()]  # noqa: SLF001
    assert names == ["test"]


def test_provider_chain_empty_uses_legacy_single_provider():
    service = ResearchService.__new__(ResearchService)
    service.settings = _settings(research_provider="youtube",
                                 youtube_api_key="fake-key")
    names = [p.name for p in service._provider_chain()]  # noqa: SLF001
    assert names == ["youtube", "internet_archive"]
    service.settings = _settings(research_provider="internet_archive")
    names = [p.name for p in service._provider_chain()]  # noqa: SLF001
    assert names == ["internet_archive"]


def test_youtube_unavailable_falls_back_to_archive_with_note(engine):
    """YouTube listed but no key: the run still succeeds on the
    Archive, and the report says YouTube was unavailable."""
    job = _new_job(engine)
    service = ResearchService(engine, _settings())
    service._provider_chain = lambda: [  # noqa: SLF001
        YouTubeProvider(api_key=""),
        _StubProvider("internet_archive", [_raw("internet_archive", "ia-1")]),
    ]
    report = service.run(job.id)
    assert len(report.candidates) == 1
    assert report.candidates[0].external_video_id == "ia-1"
    by_provider = {r.provider: r for r in report.provider_results}
    assert by_provider["youtube"].status == "unavailable"
    assert "YOUTUBE_API_KEY" in (by_provider["youtube"].error or "")
    assert by_provider["internet_archive"].status == "ok"
    assert any("youtube" in lim.lower() and "unavailable" in lim.lower()
               for lim in report.limitations)


# ------------------------------------------------------------------ settings status
def test_effective_providers_helper():
    assert _effective_providers(_settings()) == ["internet_archive"]
    assert _effective_providers(
        _settings(research_providers="youtube,internet_archive")) == [
        "youtube", "internet_archive"]
    assert _effective_providers(
        _settings(research_provider="youtube")) == [
        "youtube", "internet_archive"]
    assert _effective_providers(
        _settings(research_providers="test,youtube")) == ["test"]


def test_settings_status_reports_effective_providers():
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        status = client.get("/settings/status").json()
        assert status["youtube_configured"] is False
        assert status["research_provider"] == "internet_archive"
        assert status["research_providers"] == ["internet_archive"]


# ------------------------------------------------------------------ YouTube download, stubbed yt-dlp
class _StubRunner:
    """Fake yt-dlp: copies a real clip into place, or fails on demand.

    Records every command so tests can prove what was (not) run:
    the watch URL, the --max-filesize cap, the output path.
    """

    def __init__(self, content=None, *, fail=False, max_filesize_skip=False):
        self.content = content
        self.fail = fail
        self.max_filesize_skip = max_filesize_skip
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout):
        self.calls.append(list(cmd))
        out_idx = cmd.index("--output") + 1
        target = Path(cmd[out_idx])
        if self.max_filesize_skip:
            return subprocess.CompletedProcess(
                cmd, 1, "",
                "ERROR: File is larger than max-filesize (524288000 bytes)")
        if self.fail:
            return subprocess.CompletedProcess(
                cmd, 1, "", "ERROR: Video unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.content or b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _downloader_with_runner(engine, runner, **overrides):
    settings = _settings(**overrides)
    return FootageDownloader(settings, ytdlp_runner=runner)


def test_youtube_downloader_downloads_and_records_footage(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    runner = _StubRunner(content)
    downloader = _downloader_with_runner(engine, runner)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "downloaded"
    assert result.bytes_downloaded == len(content)
    assert result.local_path and Path(result.local_path).is_file()
    assert result.local_path.endswith("cc-vid-1.mp4")
    assert result.source_url == "https://www.youtube.com/watch?v=cc-vid-1"
    # The command carried the watch URL and the per-file cap.
    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert "https://www.youtube.com/watch?v=cc-vid-1" in cmd
    assert cmd[cmd.index("--max-filesize") + 1] == str(int(500 * 1024 * 1024))
    assert "--merge-output-format" in cmd and "mp4" in cmd
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path == result.local_path
        assert footage.duration_seconds == pytest.approx(30.0, abs=0.5)


def test_youtube_downloader_never_downloads_unapproved_footage(engine, tmp_path):
    """A standard-licence YouTube video is rejected by Rights, so
    the gate fails and yt-dlp is never run — 0 subprocess calls."""
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "std-vid-1",
                                   licence="standard YouTube licence")
    # Rights rejects the standard licence, so the service raises
    # "approved 0 of 1" — the rejection evidence is still recorded.
    with pytest.raises(ExternalServiceError, match="approved 0 of 1"):
        RightsService(engine, _settings()).run(job.id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage is None, "standard licence must not be approved"
        evidence = (session.query(RightsEvidence)
                    .filter(RightsEvidence.candidate_video_id == candidate_id).first())
        assert evidence is not None and evidence.verdict == "rejected"
    runner = _StubRunner(b"")
    downloader = _downloader_with_runner(engine, runner)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "rights gate" in result.reason
    assert result.local_path is None
    assert runner.calls == [], "yt-dlp must never run for unapproved footage"


def test_youtube_downloader_ytdlp_failure_leaves_no_file(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    runner = _StubRunner(fail=True)
    downloader = _downloader_with_runner(engine, runner)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "yt-dlp failed" in result.reason
    assert result.local_path is None
    storage = Path(downloader.storage.root)
    assert not list(storage.glob(f"jobs/{job.id}/footage/{candidate_id}/*")), \
        "a failed download must not leave a partial file behind"


def test_youtube_downloader_max_filesize_skip(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    runner = _StubRunner(max_filesize_skip=True)
    downloader = _downloader_with_runner(engine, runner)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "skipped"
    assert "size cap" in result.reason
    assert result.local_path is None


def test_youtube_downloader_missing_ytdlp_is_a_clear_failure(engine, tmp_path, monkeypatch):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    runner = _StubRunner(b"")
    downloader = _downloader_with_runner(engine, runner)
    monkeypatch.setattr(FootageDownloader, "_resolve_ytdlp",
                        lambda self: None)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "yt-dlp is not installed" in result.reason
    assert runner.calls == []


def test_youtube_downloader_rejects_unsafe_video_id(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "../../etc/passwd",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    runner = _StubRunner(b"")
    downloader = _downloader_with_runner(engine, runner)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "unsafe YouTube video id" in result.reason
    assert runner.calls == []


def test_youtube_downloader_rejects_corrupt_download(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    runner = _StubRunner(b"this is not a video" * 100)
    downloader = _downloader_with_runner(engine, runner)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "ffprobe" in result.reason
    storage = Path(downloader.storage.root)
    assert not list(storage.glob(f"jobs/{job.id}/footage/{candidate_id}/*")), \
        "a file that fails ffprobe must be deleted, never analysed"


# ------------------------------------------------------------------ YouTube sign-in fallback (player clients)
class _ScriptedRunner:
    """Fake yt-dlp that fails/succeeds per call in script order.

    `script` is a list of steps: "auth" (fails with the live
    sign-in error), "fail" (fails with a generic error), or "ok"
    (copies `content` into place). Records every command so tests
    can prove which player client each attempt used.
    """

    _AUTH_STDERR = (
        "ERROR: [youtube] cc-vid-1: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication. "
        "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp "
        "for how to manually pass cookies.")

    def __init__(self, script, content=b""):
        self.script = list(script)
        self.content = content
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout):
        self.calls.append(list(cmd))
        step = self.script.pop(0) if self.script else "ok"
        out_idx = cmd.index("--output") + 1
        target = Path(cmd[out_idx])
        if step == "ok":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.content)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if step == "auth":
            return subprocess.CompletedProcess(cmd, 1, "", self._AUTH_STDERR)
        return subprocess.CompletedProcess(cmd, 1, "", step)


def _player_client_of(cmd):
    """The player client a yt-dlp command used ("web" when no
    --extractor-args was passed)."""
    if "--extractor-args" not in cmd:
        return "web"
    arg = cmd[cmd.index("--extractor-args") + 1]
    return arg.split("player_client=")[-1]


def _approved_youtube_job(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    return job, candidate_id, content


def _run_download(engine, runner, job, candidate_id):
    downloader = _downloader_with_runner(engine, runner)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    return result, downloader


def test_youtube_downloader_web_success_records_web_client(engine, tmp_path):
    job, candidate_id, content = _approved_youtube_job(engine, tmp_path)
    runner = _ScriptedRunner(["ok"], content)
    result, _ = _run_download(engine, runner, job, candidate_id)
    assert result.status == "downloaded"
    assert result.player_client == "web"
    assert "via " not in (result.reason or "")
    assert len(runner.calls) == 1
    assert _player_client_of(runner.calls[0]) == "web"


def test_youtube_downloader_retries_auth_failure_with_android(engine, tmp_path):
    """The live failure: the web client is told to sign in, the
    android player client downloads the same video."""
    job, candidate_id, content = _approved_youtube_job(engine, tmp_path)
    runner = _ScriptedRunner(["auth", "ok"], content)
    result, downloader = _run_download(engine, runner, job, candidate_id)
    assert result.status == "downloaded"
    assert result.player_client == "android"
    assert "via android player client" in result.reason
    assert result.local_path and Path(result.local_path).is_file()
    assert len(runner.calls) == 2
    assert _player_client_of(runner.calls[0]) == "web"
    assert _player_client_of(runner.calls[1]) == "android"
    assert "youtube:player_client=android" in runner.calls[1]
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path == result.local_path


def test_youtube_downloader_falls_back_android_then_ios(engine, tmp_path):
    job, candidate_id, content = _approved_youtube_job(engine, tmp_path)
    runner = _ScriptedRunner(["auth", "auth", "ok"], content)
    result, _ = _run_download(engine, runner, job, candidate_id)
    assert result.status == "downloaded"
    assert result.player_client == "ios"
    assert "via ios player client" in result.reason
    assert len(runner.calls) == 3
    assert [_player_client_of(c) for c in runner.calls] == ["web", "android", "ios"]


def test_youtube_downloader_auth_failure_on_all_clients_fails_honestly(engine, tmp_path):
    job, candidate_id, content = _approved_youtube_job(engine, tmp_path)
    runner = _ScriptedRunner(["auth", "auth", "auth"], content)
    result, downloader = _run_download(engine, runner, job, candidate_id)
    assert result.status == "failed"
    assert "sign-in" in result.reason
    assert result.player_client is None
    assert result.local_path is None
    assert len(runner.calls) == 3
    storage = Path(downloader.storage.root)
    assert not list(storage.glob(f"jobs/{job.id}/footage/{candidate_id}/*")), \
        "failed attempts must not leave a partial file behind"


def test_youtube_downloader_non_auth_failure_does_not_retry(engine, tmp_path):
    """A video that is simply unavailable must not burn two more
    attempts on the android/ios clients."""
    job, candidate_id, content = _approved_youtube_job(engine, tmp_path)
    runner = _ScriptedRunner(["ERROR: [youtube] cc-vid-1: Video unavailable"],
                             content)
    result, _ = _run_download(engine, runner, job, candidate_id)
    assert result.status == "failed"
    assert "yt-dlp failed" in result.reason
    assert "sign-in" not in result.reason
    assert len(runner.calls) == 1


def test_youtube_downloader_max_filesize_skip_does_not_retry(engine, tmp_path):
    """An oversized video is a cap decision, not an auth failure —
    no fallback attempts."""
    job, candidate_id, content = _approved_youtube_job(engine, tmp_path)
    runner = _StubRunner(max_filesize_skip=True)
    result, _ = _run_download(engine, runner, job, candidate_id)
    assert result.status == "skipped"
    assert "size cap" in result.reason
    assert len(runner.calls) == 1


# ------------------------------------------------------------------ service level
def test_service_run_downloads_approved_youtube_footage(engine, tmp_path, monkeypatch):
    """End to end for one approved CC YouTube candidate: stubbed
    yt-dlp supplies a real clip, VI analyses it into 5 moments."""
    import app.agents.video_intelligence.download as download_module

    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    runner = _StubRunner(content)
    monkeypatch.setattr(download_module, "_run_ytdlp", runner)
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    service = VideoIntelligenceService(engine, _settings())
    report = service.run(job.id)
    assert report.metrics["moments"] == 5
    assert report.metrics["downloaded_candidates"] == 1
    assert report.metrics["downloaded_bytes"] == len(content)
    assert report.candidates[0].download is not None
    assert report.candidates[0].download.status == "downloaded"
    assert len(runner.calls) == 1
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path and Path(footage.storage_path).is_file()
        assert footage.storage_path.startswith(
            str(Path(service.storage.root / f"jobs/{job.id}/footage")))
