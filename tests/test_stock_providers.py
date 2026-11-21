"""Pexels + Pixabay provider tests (post-Prompt 12).

Both are free stock-video libraries with public search APIs and
free API keys (no card). Every video is released under the
platform licence (Pexels License / Pixabay Content License —
free commercial use, modification and redistribution, no
attribution required), so the providers record the platform
licence URL and Rights approves them from the catalogue. Their
videos are direct MP4 files, downloaded over HTTPS after the
Rights gate — no yt-dlp needed for these two.

No test touches the live APIs: no keys are configured on this
VM, so search and the detail re-fetch are stubbed with
MockTransport and the file bytes are a real OpenCV-generated
clip. Live verification must happen on the deploy host, via a
Render-side job, once the keys are set.
"""
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.research.providers import (
    PEXELS_LICENCE_URL,
    PIXABAY_LICENCE_URL,
    PexelsProvider,
    PixabayProvider,
    ProviderUnavailable,
    RawCandidate,
    select_pexels_file,
    select_pixabay_file,
)
from app.agents.research.service import ResearchService
from app.agents.rights.evidence import evaluate
from app.agents.rights.licenses import parse_licence
from app.agents.rights.service import RightsService
from app.agents.video_intelligence.download import FootageDownloader
from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.models.models import ApprovedFootage, CandidateVideo, Source
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


def _seed_candidate(engine, job_id, external_id, *, licence, platform,
                    title="Funny Cat Fails", creator="CatChannel", url=None):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        source = (session.query(Source)
                  .filter(Source.platform == platform,
                          Source.external_id == external_id).first())
        if source is None:
            source = Source(platform=platform, external_id=external_id,
                            title=title, url=url or f"https://example.invalid/{external_id}")
            session.add(source)
            session.flush()
        candidate = CandidateVideo(
            job_id=job_id, source_id=source.id, external_video_id=external_id,
            title=title, url=url or f"https://example.invalid/{external_id}",
            duration_seconds=30.0, creator=creator, licence=licence)
        session.add(candidate)
        session.commit()
        return candidate.id


def _approve(engine, job_id, candidate_id):
    """Approve via the real Rights service (platform licence)."""
    RightsService(engine, _settings()).run(job_id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage is not None, "rights did not approve the candidate"


# ------------------------------------------------------------------ Pexels provider, stubbed API
def _pexels_handler(*, video_id="1234567", url="https://www.pexels.com/video/funny-cat-jumps-1234567/",
                    duration=12, files=None, search_status=200):
    """MockTransport handler for the Pexels video search API.

    Records every request so tests can assert the bare
    Authorization header (not Bearer) was actually sent.
    """
    calls = {"search": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.pexels.com":
            calls["search"].append({
                "params": dict(request.url.params),
                "authorization": request.headers.get("Authorization"),
            })
            if search_status != 200:
                return httpx.Response(search_status, content=b"error")
            video_files = files if files is not None else [
                {"link": "https://videos.pexels.com/video-files/1234567/1234567-hd_1280_720_25fps.mp4",
                 "quality": "hd", "width": 1280, "height": 720,
                 "file_type": "video/mp4", "fps": 25},
                {"link": "https://videos.pexels.com/video-files/1234567/1234567-uhd_3840_2160_25fps.mp4",
                 "quality": "uhd", "width": 3840, "height": 2160,
                 "file_type": "video/mp4", "fps": 25},
            ]
            return httpx.Response(200, json={"videos": [{
                "id": int(video_id) if str(video_id).isdigit() else video_id,
                "width": 1920, "height": 1080, "url": url,
                "image": "https://images.pexels.com/videos/1234567/pictures/preview.jpg",
                "duration": duration,
                "user": {"name": "Cat Person", "url": "https://www.pexels.com/@catperson"},
                "video_files": video_files}]})
        return httpx.Response(404, content=b"not found")

    handler.calls = calls
    return handler


def _pexels_provider(handler, api_key="test-key"):
    def factory(timeout):
        return httpx.Client(transport=httpx.MockTransport(handler),
                            timeout=timeout)
    return PexelsProvider(api_key=api_key, client_factory=factory)


def test_pexels_search_sends_bare_key_and_maps_fields():
    handler = _pexels_handler()
    found = _pexels_provider(handler).search("funny cats", limit=5)
    assert len(found) == 1
    assert found[0].platform == "pexels"
    assert found[0].external_id == "1234567"
    # Title from the landing-page slug — the uploader's own words.
    assert found[0].title == "Funny Cat Jumps"
    assert found[0].creator == "Cat Person"
    assert found[0].duration_seconds == 12.0
    assert found[0].licence == PEXELS_LICENCE_URL
    assert found[0].url == "https://www.pexels.com/video/funny-cat-jumps-1234567/"
    # Bare Authorization header (not Bearer), query + per_page sent.
    assert handler.calls["search"][0]["authorization"] == "test-key"
    assert handler.calls["search"][0]["params"]["query"] == "funny cats"
    assert handler.calls["search"][0]["params"]["per_page"] == "5"


def test_pexels_title_falls_back_without_a_slug():
    handler = _pexels_handler(url="https://www.pexels.com/video/1234567/")
    found = _pexels_provider(handler).search("funny cats")
    assert found[0].title == "Pexels video 1234567"


def test_pexels_without_key_is_unavailable_not_faked():
    with pytest.raises(ProviderUnavailable, match="PEXELS_API_KEY"):
        PexelsProvider(api_key="").search("funny cats")


def test_pexels_search_http_error_raises():
    handler = _pexels_handler(search_status=401)
    with pytest.raises(httpx.HTTPStatusError):
        _pexels_provider(handler).search("funny cats")


# ------------------------------------------------------------------ Pixabay provider, stubbed API
def _pixabay_handler(*, video_id=125, tags="cat, funny, kitten", duration=12,
                     views=5000, files=None, search_status=200):
    """MockTransport handler for the Pixabay video search API."""
    calls = {"search": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pixabay.com":
            calls["search"].append(dict(request.url.params))
            if search_status != 200:
                return httpx.Response(search_status, content=b"error")
            videos = files if files is not None else {
                "large": {"url": "https://cdn.pixabay.com/video/2024/01/01/125-4k.mp4",
                          "width": 3840, "height": 2160, "size": 50000000},
                "medium": {"url": "https://cdn.pixabay.com/video/2024/01/01/125-medium.mp4",
                           "width": 1920, "height": 1080, "size": 10000000},
                "small": {"url": "https://cdn.pixabay.com/video/2024/01/01/125-small.mp4",
                          "width": 1280, "height": 720, "size": 5000000},
                "tiny": {"url": "https://cdn.pixabay.com/video/2024/01/01/125-tiny.mp4",
                         "width": 960, "height": 540, "size": 2000000},
            }
            return httpx.Response(200, json={"totalHits": 1, "hits": [{
                "id": video_id, "pageURL": "https://pixabay.com/videos/id-125/",
                "type": "film", "tags": tags, "duration": duration,
                "views": views, "downloads": 100, "user": "CatFilmer",
                "videos": videos}]})
        return httpx.Response(404, content=b"not found")

    handler.calls = calls
    return handler


def _pixabay_provider(handler, api_key="test-key"):
    def factory(timeout):
        return httpx.Client(transport=httpx.MockTransport(handler),
                            timeout=timeout)
    return PixabayProvider(api_key=api_key, client_factory=factory)


def test_pixabay_search_sends_key_param_and_maps_fields():
    handler = _pixabay_handler()
    found = _pixabay_provider(handler).search("funny cats", limit=5)
    assert len(found) == 1
    assert found[0].platform == "pixabay"
    assert found[0].external_id == "125"
    # Title from the provider-published tags.
    assert found[0].title == "cat, funny, kitten"
    assert found[0].creator == "CatFilmer"
    assert found[0].duration_seconds == 12.0
    assert found[0].view_count == 5000
    assert found[0].licence == PIXABAY_LICENCE_URL
    assert found[0].url == "https://pixabay.com/videos/id-125/"
    params = handler.calls["search"][0]
    assert params["key"] == "test-key"
    assert params["q"] == "funny cats"
    assert params["safesearch"] == "true"


def test_pixabay_without_key_is_unavailable_not_faked():
    with pytest.raises(ProviderUnavailable, match="PIXABAY_API_KEY"):
        PixabayProvider(api_key="").search("funny cats")


def test_pixabay_search_http_error_raises():
    handler = _pixabay_handler(search_status=400)
    with pytest.raises(httpx.HTTPStatusError):
        _pixabay_provider(handler).search("funny cats")


# ------------------------------------------------------------------ file selection (pure)
def test_select_pexels_file_prefers_hd_over_4k():
    files = [
        {"link": "https://videos.pexels.com/x-uhd.mp4", "quality": "uhd",
         "width": 3840, "height": 2160, "file_type": "video/mp4"},
        {"link": "https://videos.pexels.com/x-hd.mp4", "quality": "hd",
         "width": 1280, "height": 720, "file_type": "video/mp4"},
        {"link": "https://videos.pexels.com/x-sd.mp4", "quality": "sd",
         "width": 640, "height": 360, "file_type": "video/mp4"},
        {"link": "https://videos.pexels.com/x.webm", "quality": "hd",
         "width": 1920, "height": 1080, "file_type": "video/webm"},
    ]
    assert select_pexels_file(files) == "https://videos.pexels.com/x-hd.mp4"


def test_select_pexels_file_falls_back_to_smallest_over_1920():
    files = [
        {"link": "https://videos.pexels.com/x-4k.mp4", "width": 3840,
         "file_type": "video/mp4"},
        {"link": "https://videos.pexels.com/x-2560.mp4", "width": 2560,
         "file_type": "video/mp4"},
    ]
    assert select_pexels_file(files) == "https://videos.pexels.com/x-2560.mp4"


def test_select_pexels_file_none_when_no_mp4():
    assert select_pexels_file([{"link": "https://x.webm",
                                "file_type": "video/webm"}]) is None
    assert select_pexels_file(None) is None
    assert select_pexels_file([]) is None


def test_select_pixabay_file_prefers_medium_then_small():
    videos = {
        "large": {"url": "https://cdn.pixabay.com/large.mp4"},
        "medium": {"url": "https://cdn.pixabay.com/medium.mp4"},
        "small": {"url": "https://cdn.pixabay.com/small.mp4"},
        "tiny": {"url": "https://cdn.pixabay.com/tiny.mp4"},
    }
    assert select_pixabay_file(videos) == "https://cdn.pixabay.com/medium.mp4"
    del videos["medium"]
    assert select_pixabay_file(videos) == "https://cdn.pixabay.com/small.mp4"
    del videos["small"]
    assert select_pixabay_file(videos) == "https://cdn.pixabay.com/tiny.mp4"
    del videos["tiny"]
    assert select_pixabay_file(videos) == "https://cdn.pixabay.com/large.mp4"
    assert select_pixabay_file({"medium": {"url": ""}}) is None
    assert select_pixabay_file(None) is None


# ------------------------------------------------------------------ rights: stock licences
def test_rights_approves_pexels_licence():
    decision = evaluate(licence_raw=PEXELS_LICENCE_URL,
                        title="Funny Cat Jumps", creator="Cat Person",
                        url="https://www.pexels.com/video/funny-cat-jumps-1234567/",
                        platform="pexels")
    assert decision.verdict == "approved"
    assert decision.commercial_use and decision.modification
    assert decision.redistribution
    assert not decision.attribution_required
    assert decision.licence_name == "Pexels License"


def test_rights_approves_pixabay_licence():
    decision = evaluate(licence_raw=PIXABAY_LICENCE_URL,
                        title="cat, funny, kitten", creator="CatFilmer",
                        url="https://pixabay.com/videos/id-125/",
                        platform="pixabay")
    assert decision.verdict == "approved"
    assert decision.commercial_use and decision.modification
    assert decision.redistribution
    assert not decision.attribution_required
    assert decision.licence_name == "Pixabay Content License"


def test_parse_licence_recognises_stock_licences():
    assert parse_licence("Pexels License").key == "pexels-license"
    assert parse_licence("https://www.pexels.com/license/").key == "pexels-license"
    assert parse_licence("Pixabay Content License").key == "pixabay-license"
    assert parse_licence("https://pixabay.com/service/license/").key == "pixabay-license"


# ------------------------------------------------------------------ multi-provider research
class _StubProvider:
    """Deterministic in-memory provider for chain tests."""

    def __init__(self, name, candidates):
        self.name = name
        self._candidates = candidates

    def search(self, query, limit=25):
        return list(self._candidates)


def _raw(platform, external_id, *, title, duration=30.0, licence=None):
    return RawCandidate(
        platform=platform, external_id=external_id, title=title,
        creator="Creator", description="funny cats everywhere",
        url=(f"https://www.pexels.com/video/{external_id}/"
             if platform == "pexels"
             else f"https://pixabay.com/videos/id-{external_id}/"
             if platform == "pixabay"
             else f"https://archive.org/details/{external_id}"),
        duration_seconds=duration, view_count=100,
        published_at=None, licence=licence)


def test_provider_chain_four_providers_merge(engine):
    """Pexels + Pixabay + Archive in one run: distinct clips from
    all three stay, and the stock clips carry their licences."""
    job = _new_job(engine)
    pexels = _StubProvider("pexels", [
        _raw("pexels", "1234567", title="Funny Cat Jumps",
             licence=PEXELS_LICENCE_URL)])
    pixabay = _StubProvider("pixabay", [
        _raw("pixabay", "125", title="cat, funny, kitten",
             licence=PIXABAY_LICENCE_URL)])
    archive = _StubProvider("internet_archive", [
        _raw("internet_archive", "ia-1", title="Archive Cats")])
    service = ResearchService(engine, _settings())
    service._provider_chain = lambda: [pexels, pixabay, archive]  # noqa: SLF001
    report = service.run(job.id)
    kept_ids = {c.external_video_id for c in report.candidates}
    assert kept_ids == {"1234567", "125", "ia-1"}
    by_id = {c.external_video_id: c for c in report.candidates}
    assert by_id["1234567"].licence == PEXELS_LICENCE_URL
    assert by_id["125"].licence == PIXABAY_LICENCE_URL
    providers = {r.provider for r in report.provider_results if r.status == "ok"}
    assert providers == {"pexels", "pixabay", "internet_archive"}


def test_provider_chain_stock_setting_and_build_provider():
    from app.agents.research.providers import build_provider

    service = ResearchService.__new__(ResearchService)
    service.settings = _settings(
        research_providers="pexels,pixabay,youtube,internet_archive",
        pexels_api_key="fake-pexels", pixabay_api_key="fake-pixabay",
        youtube_api_key="fake-youtube")
    names = [p.name for p in service._provider_chain()]  # noqa: SLF001
    assert names == ["pexels", "pixabay", "youtube", "internet_archive"]
    assert isinstance(build_provider("pexels", service.settings), PexelsProvider)
    assert isinstance(build_provider("pixabay", service.settings), PixabayProvider)
    # Legacy single-provider setting also accepts the stock names.
    service.settings = _settings(research_provider="pexels",
                                 pexels_api_key="fake-pexels")
    names = [p.name for p in service._provider_chain()]  # noqa: SLF001
    assert names == ["pexels", "internet_archive"]


def test_stock_unavailable_falls_back_with_note(engine):
    """Pexels listed but no key: the run still succeeds on the
    Archive, and the report says Pexels was unavailable."""
    job = _new_job(engine)
    service = ResearchService(engine, _settings())
    service._provider_chain = lambda: [  # noqa: SLF001
        PexelsProvider(api_key=""),
        _StubProvider("internet_archive", [
            _raw("internet_archive", "ia-1", title="Archive Cats")]),
    ]
    report = service.run(job.id)
    assert len(report.candidates) == 1
    assert report.candidates[0].external_video_id == "ia-1"
    by_provider = {r.provider: r for r in report.provider_results}
    assert by_provider["pexels"].status == "unavailable"
    assert "PEXELS_API_KEY" in (by_provider["pexels"].error or "")
    assert any("pexels" in lim.lower() and "unavailable" in lim.lower()
               for lim in report.limitations)


def test_settings_status_reports_stock_flags():
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        status = client.get("/settings/status").json()
        assert status["pexels_configured"] is False
        assert status["pixabay_configured"] is False


# ------------------------------------------------------------------ stock download, stubbed API + file
def _stock_handler(content: bytes, *, platform, video_id, files=None,
                   detail_status=200):
    """MockTransport handler serving the provider detail API and
    the direct MP4 file. Records every request so tests can prove
    the key was sent and the gate ran before any network call."""
    calls = {"count": 0, "detail": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        host = request.url.host
        if platform == "pexels" and host == "api.pexels.com":
            calls["detail"].append(dict(request.headers))
            if detail_status != 200:
                return httpx.Response(detail_status, content=b"error")
            video_files = files if files is not None else [
                {"link": "https://videos.pexels.com/video-files/1234567/1234567-hd_1280_720_25fps.mp4",
                 "quality": "hd", "width": 1280, "height": 720,
                 "file_type": "video/mp4", "fps": 25}]
            return httpx.Response(200, json={"video_files": video_files})
        if platform == "pixabay" and host == "pixabay.com":
            calls["detail"].append(dict(request.url.params))
            if detail_status != 200:
                return httpx.Response(detail_status, content=b"error")
            videos = files if files is not None else {
                "medium": {"url": "https://cdn.pixabay.com/video/2024/01/01/125-medium.mp4",
                           "width": 1920, "height": 1080, "size": 10000000},
                "small": {"url": "https://cdn.pixabay.com/video/2024/01/01/125-small.mp4",
                          "width": 1280, "height": 720, "size": 5000000},
            }
            return httpx.Response(200, json={"totalHits": 1, "hits": [{"videos": videos}]})
        if host in ("videos.pexels.com", "cdn.pixabay.com"):
            return httpx.Response(200, content=content,
                                  headers={"Content-Length": str(len(content))})
        return httpx.Response(404, content=b"not found")

    handler.calls = calls
    return handler


def _mock_client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(timeout, _base_url):
        return httpx.Client(transport=transport, timeout=timeout)

    return factory


def _downloader_with_mock(handler, **overrides):
    settings = _settings(pexels_api_key="test-pexels",
                         pixabay_api_key="test-pixabay", **overrides)
    return FootageDownloader(settings, client_factory=_mock_client_factory(handler))


def test_pexels_downloader_downloads_and_records_footage(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "1234567", licence=PEXELS_LICENCE_URL, platform="pexels",
        url="https://www.pexels.com/video/funny-cat-jumps-1234567/")
    _approve(engine, job.id, candidate_id)
    handler = _stock_handler(content, platform="pexels", video_id="1234567")
    downloader = _downloader_with_mock(handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "downloaded"
    assert result.bytes_downloaded == len(content)
    assert result.local_path and Path(result.local_path).is_file()
    assert result.local_path.endswith("1234567-hd_1280_720_25fps.mp4")
    assert result.source_url == "https://videos.pexels.com/video-files/1234567/1234567-hd_1280_720_25fps.mp4"
    # The detail lookup carried the bare API key.
    assert handler.calls["detail"][0]["authorization"] == "test-pexels"
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path == result.local_path
        assert footage.duration_seconds == pytest.approx(30.0, abs=0.5)


def test_pixabay_downloader_downloads_and_records_footage(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "125", licence=PIXABAY_LICENCE_URL, platform="pixabay",
        url="https://pixabay.com/videos/id-125/")
    _approve(engine, job.id, candidate_id)
    handler = _stock_handler(content, platform="pixabay", video_id="125")
    downloader = _downloader_with_mock(handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "downloaded"
    assert result.bytes_downloaded == len(content)
    assert result.local_path and Path(result.local_path).is_file()
    assert result.local_path.endswith("125-medium.mp4")
    assert result.source_url == "https://cdn.pixabay.com/video/2024/01/01/125-medium.mp4"
    # The detail lookup carried the key as a query param.
    assert handler.calls["detail"][0]["key"] == "test-pixabay"
    assert handler.calls["detail"][0]["id"] == "125"
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path == result.local_path


def test_stock_downloader_never_downloads_unapproved_footage(engine, tmp_path):
    """A Pexels candidate with no licence is needs_review, so the
    gate fails and no HTTP request is made at all — 0 calls."""
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "1234567", licence=None, platform="pexels",
        url="https://www.pexels.com/video/funny-cat-jumps-1234567/")
    with pytest.raises(ExternalServiceError, match="approved 0 of 1"):
        RightsService(engine, _settings()).run(job.id)
    handler = _stock_handler(b"", platform="pexels", video_id="1234567")
    downloader = _downloader_with_mock(handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "rights gate" in result.reason
    assert result.local_path is None
    assert handler.calls["count"] == 0, "no network call before the rights gate"


def test_stock_downloader_missing_key_is_a_clear_failure(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "1234567", licence=PEXELS_LICENCE_URL, platform="pexels",
        url="https://www.pexels.com/video/funny-cat-jumps-1234567/")
    _approve(engine, job.id, candidate_id)
    handler = _stock_handler(b"", platform="pexels", video_id="1234567")
    # No key in settings: the detail lookup cannot run.
    settings = _settings(pexels_api_key="", pixabay_api_key="")
    downloader = FootageDownloader(
        settings, client_factory=_mock_client_factory(handler))
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "not configured" in result.reason
    assert handler.calls["count"] == 0


def test_stock_downloader_no_mp4_file_is_a_clear_failure(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "1234567", licence=PEXELS_LICENCE_URL, platform="pexels",
        url="https://www.pexels.com/video/funny-cat-jumps-1234567/")
    _approve(engine, job.id, candidate_id)
    handler = _stock_handler(
        b"", platform="pexels", video_id="1234567",
        files=[{"link": "https://videos.pexels.com/x.webm",
                "quality": "hd", "width": 1920, "file_type": "video/webm"}])
    downloader = _downloader_with_mock(handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "no downloadable MP4" in result.reason
    assert result.local_path is None


def test_stock_downloader_oversize_file_is_rejected(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "125", licence=PIXABAY_LICENCE_URL, platform="pixabay",
        url="https://pixabay.com/videos/id-125/")
    _approve(engine, job.id, candidate_id)
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    handler = _stock_handler(content, platform="pixabay", video_id="125")
    # Tiny per-file cap: the declared Content-Length trips it.
    downloader = _downloader_with_mock(handler, footage_max_mb=0.0001)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "over the" in result.reason and "limit" in result.reason
    assert result.local_path is None
    storage = Path(downloader.storage.root)
    assert not list(storage.glob(f"jobs/{job.id}/footage/{candidate_id}/*")), \
        "an oversize download must not leave a partial file behind"
