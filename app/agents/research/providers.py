"""Momentrix AI — Research Agent, Phase 2: source discovery providers (Prompt 03).

Provider interface
------------------
Every provider implements ``search(query, limit) -> list[RawCandidate]``
and has a ``name``. A provider returns *discovery* records only:
an identifier, title, creator, description, URL and whatever licence
metadata the provider itself publishes. A search result is never
presented as downloadable or reusable — that is the Rights
Verification Agent's job (Prompt 04).

Real vs test providers (verified 2026-11-20, this VM)
-------------------------------------------------------
* ``internet_archive`` — REAL, no key. Internet Archive
  AdvancedSearch (``mediatype:movies``) + the public metadata API
  for durations. Verified live: 1,869 parkour items, 565 "funny cats",
  541 "space rockets" (title/description-fielded search). Returns
  real identifiers, titles, creators, descriptions, publication
  dates, download counts and licence URLs when the item publishes one.
* ``youtube`` — Creative Commons only, needs a key. YouTube Data
  API v3 (search + videos.list). Search is filtered to
  ``videoLicense=creativeCommon`` and each video's ``status.license``
  is fetched: YouTube's only Creative Commons option is CC BY 3.0
  ("Creative Commons Attribution license (reuse allowed)"), so a
  ``creativeCommon`` video is recorded with the CC BY 3.0 licence
  URL and Rights can approve it; a standard-licence video is
  recorded as such and Rights rejects it. Standard-licence videos
  are never presented as usable. UNVERIFIED LIVE on this VM: the
  egress proxy blocks googleapis.com (connection fails) and
  youtube.com (TLS verify fails through the MITM), so live
  verification must happen on the deploy host (Render).
* ``pexels`` / ``pixabay`` — free stock-video libraries, need a
  free key (no card). Pexels: ``GET api.pexels.com/videos/search``,
  bare ``Authorization: <key>`` header, 200 requests/hour.
  Pixabay: ``GET pixabay.com/api/videos/``, ``key`` query param,
  100 requests/60 s. Both publish short clips with durations, and
  every video is released under the platform licence (Pexels
  License / Pixabay Content License — free commercial use,
  modification and redistribution, no attribution required), so
  the providers record the platform licence URL and Rights can
  approve them from the catalogue. Neither API publishes a
  per-video licence field or a title: Pexels titles are derived
  from the landing-page URL slug (the uploader's own words, or
  "Pexels video {id}" when there is no slug), Pixabay titles from
  the provider-published tags. Both are recorded as provider
  metadata, never invented. UNVERIFIED LIVE on this VM (no keys
  configured); live verification must happen on the deploy host
  once the keys are set.
* ``test`` — TEST-ONLY deterministic provider. In-memory candidates
  derived from the query, labelled ``test-only`` in the licence field
  and served from ``example.invalid`` URLs. Used only when
  ``RESEARCH_PROVIDER=test`` is set explicitly (tests / orchestrator
  integration). Never the default, never presented as real sources.

Evaluated and NOT used: Openverse (images + audio only, no video
endpoint — verified live), Wikimedia Commons (mostly still images;
video search exists but duration / licence metadata is inconsistent).

Environment quirk (documented, not hidden): outbound traffic in
this VM goes through an egress proxy that MITMs TLS with a
self-signed certificate, and httpx's ``trust_env`` parsing chokes
on the IPv6 entries in ``NO_PROXY``. So the HTTP client is built
with ``trust_env=False``, the proxy taken explicitly from
``HTTPS_PROXY``/``HTTP_PROXY``, and TLS verified against the bundle
in ``SSL_CERT_FILE`` (the egress CA bundle) or the system bundle.
That is a transport detail of this VM, not a weakening of
verification: certificates are still verified, never disabled.
"""
from __future__ import annotations

import os
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.errors import ExternalServiceError

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".ogv", ".m4v"}


class ProviderUnavailable(ExternalServiceError):
    """A discovery provider cannot be used right now (no key, down, …)."""

    code = "provider_unavailable"
    status_code = 502


@dataclass
class RawCandidate:
    """One un-scored discovery record, exactly as a provider returned it."""

    platform: str
    external_id: str
    title: str | None = None
    creator: str | None = None
    description: str | None = None
    url: str | None = None
    duration_seconds: float | None = None
    view_count: int | None = None
    published_at: datetime | None = None
    licence: str | None = None
    provider: str = ""
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider:
            self.provider = self.platform


def _http_client(timeout: float) -> httpx.Client:
    """httpx client that works through this VM's egress proxy.

    See the module docstring: trust_env=False avoids httpx choking
    on the IPv6 NO_PROXY entries; the proxy is passed explicitly;
    TLS is verified against the egress CA bundle when present.
    """
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
    bundle = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/certs/ca-certificates.crt"
    if bundle and Path(bundle).exists():
        # ssl context (not verify=<str>, which httpx deprecates).
        verify = ssl.create_default_context(cafile=bundle)
    else:
        verify = True
    return httpx.Client(timeout=timeout, trust_env=False, proxy=proxy,
                        verify=verify, follow_redirects=False)


def _parse_publicdate(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return " ".join(parts) or None
    return None


# ------------------------------------------------------------------ Internet Archive
class InternetArchiveProvider:
    """Real provider: archive.org AdvancedSearch + metadata API. No key."""

    name = "internet_archive"
    base_url = "https://archive.org"

    def __init__(self, timeout: float = 15.0, max_attempts: int = 3):
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)

    def search(self, query: str, limit: int = 25) -> list[RawCandidate]:
        """Fielded title/description search over public-domain / CC movies.

        Verified live 2026-11-20: a full-text query returns unrelated
        items at the top (sorted by downloads); restricting to
        ``title:(…) OR description:(…)`` puts the exact subject first.
        """
        terms = " ".join(query.split())
        if not terms:
            raise ProviderUnavailable("internet_archive: empty search query")
        ia_query = f"mediatype:movies AND (title:({terms}) OR description:({terms}))"
        params = {
            "q": ia_query,
            "fl[]": ["identifier", "title", "creator", "description",
                     "publicdate", "downloads", "licenseurl"],
            "rows": max(1, limit),
            "output": "json",
            "sort[]": "downloads desc",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with _http_client(self.timeout) as client:
                    response = client.get(f"{self.base_url}/advancedsearch.php", params=params)
                response.raise_for_status()
                docs = response.json()["response"]["docs"]
                return [self._to_candidate(d) for d in docs if d.get("identifier")]
            except Exception as exc:  # noqa: BLE001 — retry, then report
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
        raise ProviderUnavailable(
            f"internet_archive search failed after {self.max_attempts} attempt(s): {last_error}")

    @staticmethod
    def _to_candidate(doc: dict) -> RawCandidate:
        identifier = str(doc["identifier"])
        downloads = doc.get("downloads")
        return RawCandidate(
            platform="internet_archive",
            external_id=identifier,
            title=_as_text(doc.get("title")) or identifier,
            creator=_as_text(doc.get("creator")),
            description=_as_text(doc.get("description")),
            url=f"https://archive.org/details/{identifier}",
            view_count=int(downloads) if isinstance(downloads, (int, float)) else None,
            published_at=_parse_publicdate(doc.get("publicdate")),
            licence=_as_text(doc.get("licenseurl")),
            raw=dict(doc),
        )

    def enrich_duration(self, external_id: str) -> float | None:
        """Duration (seconds) from the item's metadata API, or None.

        Reads the longest video file's ``length`` field. A failure
        here never fails the search — the candidate is kept with
        duration unknown and a lower completeness score.
        """
        try:
            with _http_client(10.0) as client:
                response = client.get(f"{self.base_url}/metadata/{external_id}")
            response.raise_for_status()
            files = response.json().get("files") or []
        except Exception:  # noqa: BLE001 — duration is enrichment, not fatal
            return None
        best: float | None = None
        for file in files:
            name = str(file.get("name", ""))
            if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            try:
                length = float(file.get("length") or 0)
            except (TypeError, ValueError):
                continue
            if length > 0 and (best is None or length > best):
                best = length
        return best


# ------------------------------------------------------------------ YouTube (key required)
# YouTube's only Creative Commons option is CC BY 3.0 — "Creative
# Commons Attribution license (reuse allowed)". A video whose
# status.license is "creativeCommon" is therefore recorded with
# the CC BY 3.0 licence URL, which the Rights catalogue parses and
# can approve (commercial use, modification, redistribution, with
# attribution). A standard-licence video is recorded as such and
# Rights rejects it. The licence string is never invented: it comes
# from the API's own status.license field.
YOUTUBE_CC_LICENCE_URL = "https://creativecommons.org/licenses/by/3.0/"
YOUTUBE_STANDARD_LICENCE = "standard YouTube licence"


class YouTubeProvider:
    """YouTube Data API v3, Creative Commons only.

    Search is filtered to ``videoLicense=creativeCommon`` and each
    video's ``status.license`` is fetched via videos.list. Disabled
    without a key: no ``YOUTUBE_API_KEY`` means ``search()`` raises
    ``ProviderUnavailable`` instead of returning anything — results
    are never fabricated. ``client_factory`` is a test seam: a
    callable taking a timeout and returning an httpx client (e.g.
    one built on a MockTransport). Production always uses the real
    client. UNVERIFIED LIVE on this VM: googleapis.com is
    unreachable through the egress proxy, so live verification must
    happen on the deploy host."""

    name = "youtube"

    def __init__(self, api_key: str = "", timeout: float = 15.0,
                 client_factory=None):
        self.api_key = api_key
        self.timeout = timeout
        self._client_factory = client_factory or _http_client

    def _client(self):
        return self._client_factory(self.timeout)

    def search(self, query: str, limit: int = 25) -> list[RawCandidate]:
        if not self.api_key:
            raise ProviderUnavailable(
                "youtube: YOUTUBE_API_KEY is not configured; "
                "YouTube discovery is unavailable, not faked")
        params = {"part": "snippet", "type": "video", "q": query,
                  "videoLicense": "creativeCommon",
                  "maxResults": max(1, min(limit, 50)), "key": self.api_key}
        with self._client() as client:
            response = client.get("https://www.googleapis.com/youtube/v3/search", params=params)
        response.raise_for_status()
        items = response.json().get("items", [])
        ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
        details = self._video_details(ids) if ids else {}
        out: list[RawCandidate] = []
        for item in items:
            video_id = item.get("id", {}).get("videoId")
            if not video_id:
                continue
            snippet = item.get("snippet", {})
            detail = details.get(video_id, {})
            out.append(RawCandidate(
                platform="youtube", external_id=video_id,
                title=snippet.get("title"), creator=snippet.get("channelTitle"),
                description=snippet.get("description"),
                url=f"https://www.youtube.com/watch?v={video_id}",
                duration_seconds=detail.get("duration_seconds"),
                view_count=detail.get("view_count"),
                published_at=_parse_publicdate(snippet.get("publishedAt")),
                licence=_licence_for_status(detail.get("licence")),
                raw=dict(item)))
        return out

    def _video_details(self, video_ids: list[str]) -> dict[str, dict]:
        import re

        params = {"part": "contentDetails,statistics,status",
                  "id": ",".join(video_ids), "key": self.api_key}
        with self._client() as client:
            response = client.get("https://www.googleapis.com/youtube/v3/videos", params=params)
        response.raise_for_status()
        out: dict[str, dict] = {}
        for item in response.json().get("items", []):
            duration = item.get("contentDetails", {}).get("duration", "")
            match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
            seconds = None
            if match:
                h, m, s = (int(match.group(i) or 0) for i in (1, 2, 3))
                seconds = float(h * 3600 + m * 60 + s)
            stats = item.get("statistics", {})
            out[item["id"]] = {
                "duration_seconds": seconds,
                "view_count": int(stats["viewCount"]) if stats.get("viewCount", "").isdigit() else None,
                "licence": item.get("status", {}).get("license"),
            }
        return out


def _licence_for_status(status_licence: str | None) -> str | None:
    """Map a YouTube videos.list status.license value to the licence
    string Research stores (verbatim provider-published evidence).

    "creativeCommon" is YouTube's CC BY 3.0 grant; "youtube" is the
    standard licence (viewing only — Rights rejects it); anything
    else is unknown, so None (Rights will mark it needs_review).
    """
    if status_licence == "creativeCommon":
        return YOUTUBE_CC_LICENCE_URL
    if status_licence == "youtube":
        return YOUTUBE_STANDARD_LICENCE
    return None


# ------------------------------------------------------------------ Pexels / Pixabay (free key)
# Both are free stock-video libraries. Every video is released
# under the platform licence (Pexels License / Pixabay Content
# License: free commercial use, modification and redistribution,
# no attribution required), so the provider records the platform
# licence URL — the published terms — and Rights approves it from
# the catalogue. Neither API publishes a per-video licence field,
# so the licence string is the platform terms, never invented
# per-video metadata. The full API response is kept in `raw` for
# audit. Direct MP4 file links are selected at download time
# (see `select_pexels_file` / `select_pixabay_file`, used by the
# footage downloader); the search response itself carries no
# downloadable file, only the landing-page URL.
PEXELS_LICENCE_URL = "https://www.pexels.com/license/"
PIXABAY_LICENCE_URL = "https://pixabay.com/service/license/"


def _title_from_pexels_url(url: str | None, video_id: str) -> str:
    """A Pexels video title from its landing-page URL slug.

    Pexels publishes no title field. The landing page URL usually
    carries the uploader's own descriptive slug, e.g.
    https://www.pexels.com/video/funny-cat-jumps-1234567/ ->
    "Funny Cat Jumps". A bare numeric URL has no slug, so the
    fallback is "Pexels video {id}" — never an invented title.
    """
    if url:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"-\d+$", "", slug)  # strip the trailing numeric id
        words = [w for w in slug.split("-") if w and not w.isdigit()]
        if words:
            return " ".join(words).title()
    return f"Pexels video {video_id}"


def select_pexels_file(video_files: list | None) -> str | None:
    """Pick the direct MP4 file link from a Pexels `video_files` list.

    MP4 only. Prefer the largest file at most 1920 px wide (Full HD
    is the most the render needs); if every file is wider than that,
    take the smallest of them (keeps 4K bloat out of the download).
    Returns the file link, or None when no MP4 file is listed.
    """
    if not isinstance(video_files, list):
        return None
    mp4s = []
    for entry in video_files:
        if not isinstance(entry, dict):
            continue
        link = str(entry.get("link") or "").strip()
        if not link or entry.get("file_type") != "video/mp4":
            continue
        try:
            width = int(entry.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        mp4s.append((width, link))
    if not mp4s:
        return None
    under = [item for item in mp4s if 0 < item[0] <= 1920]
    if under:
        return max(under, key=lambda item: item[0])[1]
    return min(mp4s, key=lambda item: item[0])[1]


def select_pixabay_file(videos: dict | None) -> str | None:
    """Pick the direct MP4 file link from a Pixabay `videos` object.

    Tiers in order: medium (1920x1080, always available — exactly
    what the render needs), small (1280x720), tiny, large (4K,
    not always available). Returns the file URL, or None when no
    tier carries one.
    """
    if not isinstance(videos, dict):
        return None
    for tier in ("medium", "small", "tiny", "large"):
        entry = videos.get(tier)
        if isinstance(entry, dict):
            url = str(entry.get("url") or "").strip()
            if url:
                return url
    return None


class PexelsProvider:
    """Pexels video search. Free key (no card), 200 requests/hour.

    ``GET https://api.pexels.com/videos/search`` with a bare
    ``Authorization: <key>`` header (not Bearer). Disabled without
    a key: ``search()`` raises ``ProviderUnavailable`` instead of
    returning anything. ``client_factory`` is the test seam, as for
    YouTube. UNVERIFIED LIVE on this VM (no key configured).
    """

    name = "pexels"

    def __init__(self, api_key: str = "", timeout: float = 15.0,
                 client_factory=None):
        self.api_key = api_key
        self.timeout = timeout
        self._client_factory = client_factory or _http_client

    def _client(self):
        return self._client_factory(self.timeout)

    def search(self, query: str, limit: int = 25) -> list[RawCandidate]:
        if not self.api_key:
            raise ProviderUnavailable(
                "pexels: PEXELS_API_KEY is not configured; "
                "Pexels discovery is unavailable, not faked")
        params = {"query": query, "per_page": max(1, min(limit, 80)),
                  "page": 1}
        with self._client() as client:
            response = client.get(
                "https://api.pexels.com/videos/search", params=params,
                headers={"Authorization": self.api_key})
        response.raise_for_status()
        return [self._to_candidate(v) for v in
                response.json().get("videos", []) if v.get("id")]

    @staticmethod
    def _to_candidate(video: dict) -> RawCandidate:
        video_id = str(video["id"])
        user = video.get("user") or {}
        duration = video.get("duration")
        try:
            duration_seconds = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_seconds = None
        return RawCandidate(
            platform="pexels", external_id=video_id,
            title=_title_from_pexels_url(video.get("url"), video_id),
            creator=_as_text(user.get("name")),
            description=None,  # Pexels publishes no description
            url=video.get("url"),
            duration_seconds=duration_seconds,
            view_count=None,  # Pexels publishes no view count
            published_at=None,
            licence=PEXELS_LICENCE_URL,
            raw=dict(video))


class PixabayProvider:
    """Pixabay video search. Free key (no card), 100 requests/60 s.

    ``GET https://pixabay.com/api/videos/`` with the ``key`` query
    param. ``safesearch=true`` is sent: stock search for a
    family-friendly Short must not return explicit material.
    Disabled without a key: ``search()`` raises
    ``ProviderUnavailable``. ``client_factory`` is the test seam.
    UNVERIFIED LIVE on this VM (no key configured).
    """

    name = "pixabay"

    def __init__(self, api_key: str = "", timeout: float = 15.0,
                 client_factory=None):
        self.api_key = api_key
        self.timeout = timeout
        self._client_factory = client_factory or _http_client

    def _client(self):
        return self._client_factory(self.timeout)

    def search(self, query: str, limit: int = 25) -> list[RawCandidate]:
        if not self.api_key:
            raise ProviderUnavailable(
                "pixabay: PIXABAY_API_KEY is not configured; "
                "Pixabay discovery is unavailable, not faked")
        params = {"key": self.api_key, "q": query,
                  "per_page": max(3, min(limit, 200)),
                  "video_type": "all", "safesearch": "true"}
        with self._client() as client:
            response = client.get("https://pixabay.com/api/videos/",
                                  params=params)
        response.raise_for_status()
        return [self._to_candidate(h) for h in
                response.json().get("hits", []) if h.get("id")]

    @staticmethod
    def _to_candidate(hit: dict) -> RawCandidate:
        video_id = str(hit["id"])
        # Pixabay publishes no title: the tags are the searchable
        # metadata, so they are the title (readable as-is).
        tags = _as_text(hit.get("tags")) or f"Pixabay video {video_id}"
        duration = hit.get("duration")
        try:
            duration_seconds = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_seconds = None
        views = hit.get("views")
        return RawCandidate(
            platform="pixabay", external_id=video_id,
            title=tags,
            creator=_as_text(hit.get("user")),
            description=None,  # Pixabay publishes no description
            url=hit.get("pageURL"),
            duration_seconds=duration_seconds,
            view_count=int(views) if isinstance(views, (int, float)) else None,
            published_at=None,
            licence=PIXABAY_LICENCE_URL,
            raw=dict(hit))


# ------------------------------------------------------------------ TEST-ONLY provider
class TestProvider:
    """TEST-ONLY deterministic provider.

    Returns in-memory candidates derived from the query. Every record
    is labelled ``test-only`` and points at ``example.invalid`` — it
    can never be mistaken for a real source. Used only when
    ``RESEARCH_PROVIDER=test`` is set explicitly (tests / orchestrator
    integration). Never the default.
    """

    name = "test"

    def search(self, query: str, limit: int = 25) -> list[RawCandidate]:
        slug = "_".join(query.lower().split()) or "topic"
        now = datetime.now(timezone.utc)
        return [
            RawCandidate(
                platform="test", external_id=f"test-{slug}-{i}",
                title=f"{query.title()} — Test Clip {i}",
                creator="test-creator",
                description=f"TEST-ONLY deterministic candidate for query {query!r}. Not a real source.",
                url=f"https://example.invalid/test/test-{slug}-{i}",
                duration_seconds=60.0 + i * 10,
                view_count=1000 * i,
                published_at=now,
                licence="test-only — not a real source",
                raw={"test_only": True, "query": query, "index": i},
            )
            for i in range(1, min(limit, 5) + 1)
        ]


PROVIDER_CLASSES = {
    "internet_archive": InternetArchiveProvider,
    "youtube": YouTubeProvider,
    "pexels": PexelsProvider,
    "pixabay": PixabayProvider,
    "test": TestProvider,
}


def build_provider(name: str, settings) -> object:
    """Build a provider by name. Unknown names are a configuration error."""
    if name == "internet_archive":
        return InternetArchiveProvider(timeout=settings.research_timeout_seconds,
                                       max_attempts=settings.research_max_attempts)
    if name == "youtube":
        return YouTubeProvider(api_key=settings.youtube_api_key,
                               timeout=settings.research_timeout_seconds)
    if name == "pexels":
        return PexelsProvider(api_key=settings.pexels_api_key,
                              timeout=settings.research_timeout_seconds)
    if name == "pixabay":
        return PixabayProvider(api_key=settings.pixabay_api_key,
                               timeout=settings.research_timeout_seconds)
    if name == "test":
        return TestProvider()
    raise ValueError(f"unknown research provider: {name!r}")
