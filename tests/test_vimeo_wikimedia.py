"""Vimeo + Wikimedia Commons provider tests (2026-11-21).

Vimeo is Creative Commons only, downloadable only: search is via
the Vimeo API v3.4 with a Bearer access token, only CC-licensed
videos carrying a direct MP4 download/file link are returned, and
Rights approves them from the existing CC catalogue. Wikimedia
Commons needs no key: the Commons API (generator=search, File
namespace) returns video files only, filtered to CC BY / CC BY-SA
/ CC0 / public-domain licences from extmetadata.

Openverse, Mixkit and Coverr are NOT built: Openverse has no
video endpoint, Mixkit and Coverr have no public search API.

No test touches the live APIs: no Vimeo token is configured on
this VM, so search and the detail re-fetch are stubbed with
MockTransport and the file bytes are a real OpenCV-generated
clip. Live verification must happen on the deploy host, via a
Render-side job, once VIMEO_ACCESS_TOKEN is set.
"""
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.research.providers import (
    VimeoProvider,
    WikimediaCommonsProvider,
    ProviderUnavailable,
    build_provider,
    select_vimeo_file,
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
            "footage_download_enabled": True,
            "vimeo_access_token": "test-vimeo"}
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
    RightsService(engine, _settings()).run(job_id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage is not None, "rights did not approve the candidate"


# ------------------------------------------------------------------ Vimeo provider, stubbed API
def _vimeo_video(video_id="123456789", *, licence="https://creativecommons.org/licenses/by/4.0/",
                 name="Funny Cat Fails", duration=30, downloadable=True):
    video = {"uri": f"/videos/{video_id}", "name": name,
             "description": "funny cats", "duration": duration,
             "link": f"https://vimeo.com/{video_id}",
             "created_time": "2024-01-01T00:00:00+00:00",
             "user": {"name": "CatChannel"},
             "stats": {"plays": 4321}}
    if licence is not None:
        video["license"] = licence
    if downloadable:
        video["download"] = [
            {"link": f"https://vod.example.invalid/{video_id}-hd.mp4",
             "width": 1280, "height": 720, "size": 10000000},
            {"link": f"https://vod.example.invalid/{video_id}-4k.mp4",
             "width": 3840, "height": 2160, "size": 50000000}]
    return video


def _vimeo_handler(videos, *, search_status=200):
    calls = {"search": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.vimeo.com":
            calls["search"].append({
                "params": dict(request.url.params),
                "authorization": request.headers.get("Authorization"),
                "accept": request.headers.get("Accept"),
            })
            if search_status != 200:
                return httpx.Response(search_status, content=b"error")
            return httpx.Response(200, json={"data": videos})
        return httpx.Response(404, content=b"not found")

    handler.calls = calls
    return handler


def _vimeo_provider(handler, access_token="test-vimeo"):
    def factory(timeout):
        return httpx.Client(transport=httpx.MockTransport(handler),
                            timeout=timeout)
    return VimeoProvider(access_token=access_token, client_factory=factory)


def test_vimeo_search_returns_cc_downloadable_only():
    handler = _vimeo_handler([_vimeo_video()])
    found = _vimeo_provider(handler).search("funny cats", limit=5)
    assert len(found) == 1
    assert found[0].platform == "vimeo"
    assert found[0].external_id == "123456789"
    assert found[0].title == "Funny Cat Fails"
    assert found[0].creator == "CatChannel"
    assert found[0].duration_seconds == 30.0
    assert found[0].view_count == 4321
    assert found[0].licence == "https://creativecommons.org/licenses/by/4.0/"
    assert found[0].raw["download_url"] == "https://vod.example.invalid/123456789-hd.mp4"
    assert found[0].url == "https://vimeo.com/123456789"
    # Bearer token, API version 3.4, query + per_page sent.
    call = handler.calls["search"][0]
    assert call["authorization"] == "Bearer test-vimeo"
    assert "version=3.4" in call["accept"]
    assert call["params"]["query"] == "funny cats"
    assert call["params"]["per_page"] == "5"


def test_vimeo_standard_licence_video_is_not_returned():
    handler = _vimeo_handler([
        _vimeo_video("111", licence="All rights reserved"),
        _vimeo_video("222", licence=None),
        _vimeo_video("333", licence="https://creativecommons.org/licenses/by/4.0/"),
    ])
    found = _vimeo_provider(handler).search("funny cats")
    assert [f.external_id for f in found] == ["333"]


def test_vimeo_non_downloadable_video_is_not_returned():
    handler = _vimeo_handler([
        _vimeo_video("111", downloadable=False),
        _vimeo_video("222", downloadable=True),
    ])
    found = _vimeo_provider(handler).search("funny cats")
    assert [f.external_id for f in found] == ["222"]


def test_vimeo_cc_short_name_maps_to_canonical_url():
    handler = _vimeo_handler([_vimeo_video(licence="CC BY-SA 3.0")])
    found = _vimeo_provider(handler).search("funny cats")
    assert len(found) == 1
    assert found[0].licence == "https://creativecommons.org/licenses/by-sa/3.0/"


def test_vimeo_cc0_is_returned():
    handler = _vimeo_handler([_vimeo_video(licence="CC0")])
    found = _vimeo_provider(handler).search("funny cats")
    assert len(found) == 1
    assert found[0].licence == "https://creativecommons.org/publicdomain/zero/1.0/"


def test_vimeo_without_token_is_unavailable_not_faked():
    with pytest.raises(ProviderUnavailable, match="VIMEO_ACCESS_TOKEN"):
        VimeoProvider(access_token="").search("funny cats")


def test_vimeo_search_http_error_raises():
    handler = _vimeo_handler([_vimeo_video()], search_status=401)
    with pytest.raises(httpx.HTTPStatusError):
        _vimeo_provider(handler).search("funny cats")


def test_select_vimeo_file_prefers_hd_over_4k():
    video = {"download": [
        {"link": "https://vod.example.invalid/x-4k.mp4", "width": 3840},
        {"link": "https://vod.example.invalid/x-hd.mp4", "width": 1280},
        {"link": "https://vod.example.invalid/x.webm", "width": 1920,
         "type": "video/webm"}]}
    assert select_vimeo_file(video) == "https://vod.example.invalid/x-hd.mp4"
    assert select_vimeo_file({"files": [
        {"link": "https://vod.example.invalid/f.mp4", "width": 1920,
         "type": "video/mp4"}]}) == "https://vod.example.invalid/f.mp4"
    assert select_vimeo_file({"download": []}) is None
    assert select_vimeo_file(None) is None


# ------------------------------------------------------------------ Wikimedia Commons provider, stubbed API
def _commons_page(title="File:Funny cat.mp4", *, mime="video/mp4",
                  licence_short="CC BY 4.0",
                  licence_url="https://creativecommons.org/licenses/by/4.0/",
                  duration=45, user="CatFilmer"):
    extmetadata = {}
    if licence_short is not None:
        extmetadata["LicenseShortName"] = {"value": licence_short}
    if licence_url is not None:
        extmetadata["LicenseUrl"] = {"value": licence_url}
    extmetadata["Artist"] = {"value": "Cat Filmer"}
    return {"title": title,
            "imageinfo": [{"url": "https://upload.wikimedia.org/wikipedia/commons/f/f1/Funny_cat.mp4",
                           "descriptionurl": "https://commons.wikimedia.org/wiki/File:Funny_cat.mp4",
                           "mime": mime, "size": 10000000,
                           "timestamp": "2024-01-01T00:00:00Z",
                           "user": user, "extmetadata": extmetadata,
                           "metadata": [{"name": "duration", "value": duration}]}]}


def _commons_handler(pages, *, search_status=200):
    calls = {"search": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "commons.wikimedia.org":
            calls["search"].append(dict(request.url.params))
            if search_status != 200:
                return httpx.Response(search_status, content=b"error")
            return httpx.Response(200, json={"query": {"pages": pages}})
        return httpx.Response(404, content=b"not found")

    handler.calls = calls
    return handler


def _commons_provider(handler):
    def factory(timeout):
        return httpx.Client(transport=httpx.MockTransport(handler),
                            timeout=timeout)
    return WikimediaCommonsProvider(client_factory=factory)


def test_commons_search_returns_cc_by_video():
    handler = _commons_handler([_commons_page()])
    found = _commons_provider(handler).search("funny cats", limit=5)
    assert len(found) == 1
    assert found[0].platform == "wikimedia_commons"
    assert found[0].external_id == "File:Funny cat.mp4"
    assert found[0].title == "Funny cat"
    assert found[0].creator == "Cat Filmer"
    assert found[0].duration_seconds == 45.0
    assert found[0].licence == "https://creativecommons.org/licenses/by/4.0/"
    assert found[0].raw["download_url"] == "https://upload.wikimedia.org/wikipedia/commons/f/f1/Funny_cat.mp4"
    assert found[0].url == "https://commons.wikimedia.org/wiki/File:Funny_cat.mp4"
    params = handler.calls["search"][0]
    assert params["generator"] == "search"
    assert params["gsrnamespace"] == "6"
    assert params["action"] == "query"


def test_commons_licence_filtering_keeps_by_sa_and_cc0_drops_nc_nd():
    handler = _commons_handler([
        _commons_page("File:BySa.webm", licence_short="CC BY-SA 4.0",
                      licence_url="https://creativecommons.org/licenses/by-sa/4.0/",
                      duration=20),
        _commons_page("File:Cc0.mp4", licence_short="CC0",
                      licence_url="https://creativecommons.org/publicdomain/zero/1.0/",
                      duration=20),
        _commons_page("File:ByNc.mp4", licence_short="CC BY-NC 4.0",
                      licence_url="https://creativecommons.org/licenses/by-nc/4.0/",
                      duration=20),
        _commons_page("File:ByNd.mp4", licence_short="CC BY-ND 4.0",
                      licence_url="https://creativecommons.org/licenses/by-nd/4.0/",
                      duration=20),
        _commons_page("File:Gfdl.mp4", licence_short="GFDL",
                      licence_url="https://www.gnu.org/licenses/fdl-1.3.html",
                      duration=20),
        _commons_page("File:Unknown.mp4", licence_short=None,
                      licence_url=None, duration=20),
    ])
    found = _commons_provider(handler).search("funny cats")
    assert [f.external_id for f in found] == ["File:BySa.webm", "File:Cc0.mp4"]
    assert found[0].licence == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert found[1].licence == "https://creativecommons.org/publicdomain/zero/1.0/"


def test_commons_video_only_filtering_drops_images_and_audio():
    handler = _commons_handler([
        _commons_page("File:Cat.jpg", mime="image/jpeg"),
        _commons_page("File:Cat.ogg", mime="audio/ogg"),
        _commons_page("File:Cat.webm", mime="video/webm", duration=10),
    ])
    found = _commons_provider(handler).search("funny cats")
    assert [f.external_id for f in found] == ["File:Cat.webm"]


def test_commons_search_http_error_raises():
    handler = _commons_handler([_commons_page()], search_status=500)
    # Retries are exhausted, then ProviderUnavailable is raised.
    with pytest.raises(ProviderUnavailable, match="wikimedia_commons search failed"):
        _commons_provider(handler).search("funny cats")


# ------------------------------------------------------------------ rights: Vimeo + Commons CC licences
def test_rights_approves_vimeo_cc_by_4():
    decision = evaluate(licence_raw="https://creativecommons.org/licenses/by/4.0/",
                        title="Funny Cat Fails", creator="CatChannel",
                        url="https://vimeo.com/123456789", platform="vimeo")
    assert decision.verdict == "approved"
    assert decision.commercial_use and decision.modification
    assert decision.redistribution and decision.attribution_required
    assert decision.licence_name == "Creative Commons Attribution 4.0"


def test_rights_approves_vimeo_cc_by_sa_and_cc0():
    for url, name in [
            ("https://creativecommons.org/licenses/by-sa/3.0/",
             "Creative Commons Attribution-ShareAlike 3.0"),
            ("https://creativecommons.org/publicdomain/zero/1.0/",
             "CC0 1.0 Universal (Public Domain Dedication)")]:
        decision = evaluate(licence_raw=url, title="Funny Cat Fails",
                            creator="CatChannel",
                            url="https://vimeo.com/123456789", platform="vimeo")
        assert decision.verdict == "approved"
        assert decision.licence_name == name


def test_rights_rejects_vimeo_cc_by_nc():
    decision = evaluate(licence_raw="https://creativecommons.org/licenses/by-nc/4.0/",
                        title="Funny Cat Fails", creator="CatChannel",
                        url="https://vimeo.com/123456789", platform="vimeo")
    assert decision.verdict == "rejected"
    assert "commercial use" in decision.reasons[0]


def test_parse_licence_recognises_commons_licences():
    assert parse_licence("https://creativecommons.org/licenses/by/4.0/").key == "cc-by-4.0"
    assert parse_licence("https://creativecommons.org/licenses/by-sa/4.0/").key == "cc-by-sa-4.0"
    assert parse_licence("https://creativecommons.org/publicdomain/zero/1.0/").key == "cc0-1.0"


# ------------------------------------------------------------------ registration + settings status
def test_build_provider_registers_vimeo_and_wikimedia():
    settings = _settings()
    assert isinstance(build_provider("vimeo", settings), VimeoProvider)
    assert isinstance(build_provider("wikimedia_commons", settings),
                     WikimediaCommonsProvider)
    assert build_provider("vimeo", settings).access_token == "test-vimeo"
    # No token: the provider builds, but search refuses to fake results.
    assert isinstance(build_provider("vimeo", _settings(vimeo_access_token="")),
                     VimeoProvider)
    service = ResearchService.__new__(ResearchService)
    service.settings = _settings(
        research_providers="vimeo,wikimedia_commons,internet_archive")
    names = [p.name for p in service._provider_chain()]  # noqa: SLF001
    assert names == ["vimeo", "wikimedia_commons", "internet_archive"]


def test_settings_status_reports_vimeo_and_wikimedia_flags():
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        status = client.get("/settings/status").json()
        assert status["vimeo_configured"] is False
        assert status["wikimedia_configured"] is True


# ------------------------------------------------------------------ download, stubbed API + file
def _download_handler(content: bytes, *, platform, video_id=None,
                      file_title=None, files=None, detail_status=200):
    """MockTransport handler serving the provider detail API and
    the direct MP4 file."""
    calls = {"count": 0, "detail": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        host = request.url.host
        if platform == "vimeo" and host == "api.vimeo.com":
            calls["detail"].append(dict(request.headers))
            if detail_status != 200:
                return httpx.Response(detail_status, content=b"error")
            download = files if files is not None else [
                {"link": "https://vod.example.invalid/123456789-hd.mp4",
                 "width": 1280, "height": 720, "size": 10000000}]
            return httpx.Response(200, json={"download": download, "files": []})
        if platform == "wikimedia_commons" and host == "commons.wikimedia.org":
            calls["detail"].append(dict(request.url.params))
            if detail_status != 200:
                return httpx.Response(detail_status, content=b"error")
            return httpx.Response(200, json={"query": {"pages": [{
                "title": file_title,
                "imageinfo": [{"url": "https://upload.wikimedia.org/wikipedia/commons/f/f1/Funny_cat.mp4",
                               "mime": "video/mp4"}]}]}})
        if host in ("vod.example.invalid", "upload.wikimedia.org"):
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
    settings = _settings(**overrides)
    return FootageDownloader(settings, client_factory=_mock_client_factory(handler))


def test_vimeo_downloader_downloads_and_records_footage(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "123456789",
        licence="https://creativecommons.org/licenses/by/4.0/",
        platform="vimeo", url="https://vimeo.com/123456789")
    _approve(engine, job.id, candidate_id)
    handler = _download_handler(content, platform="vimeo", video_id="123456789")
    downloader = _downloader_with_mock(handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "downloaded"
    assert result.bytes_downloaded == len(content)
    assert result.local_path and Path(result.local_path).is_file()
    assert result.local_path.endswith("123456789-hd.mp4")
    assert result.source_url == "https://vod.example.invalid/123456789-hd.mp4"
    assert handler.calls["detail"][0]["authorization"] == "Bearer test-vimeo"
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path == result.local_path
        assert footage.duration_seconds == pytest.approx(30.0, abs=0.5)


def test_wikimedia_downloader_downloads_and_records_footage(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "File:Funny cat.mp4",
        licence="https://creativecommons.org/licenses/by/4.0/",
        platform="wikimedia_commons",
        url="https://commons.wikimedia.org/wiki/File:Funny_cat.mp4")
    _approve(engine, job.id, candidate_id)
    handler = _download_handler(content, platform="wikimedia_commons",
                                file_title="File:Funny cat.mp4")
    downloader = _downloader_with_mock(handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "downloaded"
    assert result.bytes_downloaded == len(content)
    assert result.local_path and Path(result.local_path).is_file()
    assert result.local_path.endswith("Funny_cat.mp4")
    assert result.source_url == "https://upload.wikimedia.org/wikipedia/commons/f/f1/Funny_cat.mp4"
    assert handler.calls["detail"][0]["titles"] == "File:Funny cat.mp4"
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path == result.local_path


def test_new_providers_downloader_never_downloads_unapproved_footage(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "123456789", licence=None, platform="vimeo",
        url="https://vimeo.com/123456789")
    with pytest.raises(ExternalServiceError, match="approved 0 of 1"):
        RightsService(engine, _settings()).run(job.id)
    handler = _download_handler(b"", platform="vimeo", video_id="123456789")
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


def test_vimeo_downloader_missing_token_is_a_clear_failure(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "123456789",
        licence="https://creativecommons.org/licenses/by/4.0/",
        platform="vimeo", url="https://vimeo.com/123456789")
    _approve(engine, job.id, candidate_id)
    handler = _download_handler(b"", platform="vimeo", video_id="123456789")
    settings = _settings(vimeo_access_token="")
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


def test_vimeo_downloader_no_mp4_file_is_a_clear_failure(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "123456789",
        licence="https://creativecommons.org/licenses/by/4.0/",
        platform="vimeo", url="https://vimeo.com/123456789")
    _approve(engine, job.id, candidate_id)
    handler = _download_handler(
        b"", platform="vimeo", video_id="123456789",
        files=[{"link": "https://vod.example.invalid/x.webm",
                "width": 1920, "type": "video/webm"}])
    downloader = _downloader_with_mock(handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "no downloadable MP4" in result.reason
    assert result.local_path is None
