"""RANKFLOW AI — Research Agent, Phase 2: source discovery providers (Prompt 03).

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
* ``youtube`` — IMPLEMENTED BUT NOT VERIFIED, disabled without a key.
  YouTube Data API v3 (search + videos.list). No ``YOUTUBE_API_KEY``
  is configured in this environment, so ``search()`` raises
  ``ProviderUnavailable`` instead of returning anything. It is never
  faked: there is no scraped / guessed YouTube result anywhere.
* ``test`` — TEST-ONLY deterministic provider. In-memory candidates
  derived from the query, labelled ``test-only`` in the licence field
  and served from ``example.invalid`` URLs. Used only when
  ``RESEARCH_PROVIDER=test`` is set explicitly (tests / orchestrator
  integration). Never the default, never presented as real sources.

Evaluated and NOT used: Openverse (images + audio only, no video
endpoint — verified live), Wikimedia Commons (mostly still images;
video search exists but duration / licence metadata is inconsistent),
Pexels / Pixabay (require API keys that are not configured).

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
class YouTubeProvider:
    """YouTube Data API v3. Implemented, but NOT verified and disabled
    without a key: no YOUTUBE_API_KEY is configured in this environment.
    ``search()`` raises ProviderUnavailable instead of returning
    anything when the key is absent — results are never fabricated."""

    name = "youtube"

    def __init__(self, api_key: str = "", timeout: float = 15.0):
        self.api_key = api_key
        self.timeout = timeout

    def search(self, query: str, limit: int = 25) -> list[RawCandidate]:
        if not self.api_key:
            raise ProviderUnavailable(
                "youtube: YOUTUBE_API_KEY is not configured; "
                "YouTube discovery is unavailable, not faked")
        params = {"part": "snippet", "type": "video", "q": query,
                  "maxResults": max(1, min(limit, 50)), "key": self.api_key}
        with _http_client(self.timeout) as client:
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
                licence=None,  # YouTube licence is checked by Prompt 04, not assumed here
                raw=dict(item)))
        return out

    def _video_details(self, video_ids: list[str]) -> dict[str, dict]:
        import re

        params = {"part": "contentDetails,statistics", "id": ",".join(video_ids),
                  "key": self.api_key}
        with _http_client(self.timeout) as client:
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
            }
        return out


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
    if name == "test":
        return TestProvider()
    raise ValueError(f"unknown research provider: {name!r}")
