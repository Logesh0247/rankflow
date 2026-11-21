"""Momentrix AI — footage download for Video Intelligence (post-Prompt 12).

Fixes the live failure where a job failed at VIDEO_ANALYSIS
because an approved candidate had no local file: Research and
Rights had worked, but nothing ever downloaded the source video,
so Video Intelligence had nothing to analyse.

What this module does, and its rules:
* After Rights approves footage (or when Video Intelligence finds
  `ApprovedFootage.storage_path` empty), download the approved
  source video to local storage and record `storage_path` on the
  `ApprovedFootage` row.
* Live source is Internet Archive only: given an item identifier
  (the candidate's `external_video_id`), fetch
  `{base}/metadata/{id}`, pick a playable video file (mp4 first,
  then webm, then mov, …), stream it to disk and verify it with
  ffprobe (duration > 0) before it is ever analysed. A file that
  fails to download or probe is deleted, never analysed, never
  presented as if it existed.
* RIGHTS GATE: download ONLY footage with an approved Rights
  record. The gate is re-checked server-side
  (`require_approved`) before every download. A rejected or
  needs-review candidate is never downloaded — a failed gate
  returns a `failed` result, it never raises past the caller.
* Fail soft per file: one candidate failing to download marks
  that footage unusable and the stage moves on to the next
  approved candidate. Only when no approved candidate could be
  downloaded does the stage fail — honestly, naming counts.
* Bounds for the Render free plan: a per-file size cap and a
  per-job total cap (both configurable, see `Settings`). Files
  over the cap are skipped with a clear reason; the caps are
  enforced while streaming, so an oversized file is aborted
  mid-download instead of filling the disk.
* YouTube downloads are NOT in scope: a non-Archive platform
  is skipped with a reason saying so. YouTube search itself is
  still unconfigured.

The HTTP client mirrors the egress-proxy handling in
`app/agents/research/providers.py` (trust_env=False, proxy from
the environment, TLS verified — never disabled), except it
bypasses the proxy for loopback hosts so tests can serve
metadata and files from a local test server via
`footage_archive_base_url`.
"""
from __future__ import annotations

import os
import ssl
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from app.agents.research.providers import VIDEO_EXTENSIONS
from app.agents.rights.gate import latest_evidence, require_approved
from app.agents.video_intelligence.probe import probe_video
from app.core.config import Settings, get_settings
from app.core.errors import StorageError, ValidationError
from app.core.security import safe_join
from app.models.models import ApprovedFootage, CandidateVideo, Source
from app.storage.local import get_storage

# Extension preference order for Archive files: mp4 first, then
# webm, then mov, then the rest of the playable containers.
_EXT_PRIORITY = {".mp4": 0, ".webm": 1, ".mov": 2, ".mkv": 3,
                 ".m4v": 4, ".ogv": 5, ".avi": 6}

_CHUNK_BYTES = 1024 * 1024  # stream in 1 MB chunks


class FootageDownload(BaseModel):
    """The outcome of one footage-download attempt."""

    candidate_video_id: str
    status: str  # "downloaded" | "reused" | "skipped" | "failed"
    source_url: str | None = None
    local_path: str | None = None
    bytes_downloaded: int = 0
    reason: str | None = None


def _file_size(entry: dict) -> int | None:
    try:
        size = int(entry.get("size") or 0)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def select_archive_file(files: list[dict], max_file_bytes: int,
                        budget_bytes: int) -> tuple[dict | None, str | None]:
    """Pick the best playable video file from an Archive metadata
    `files` list, honouring both caps.

    Returns (file_entry, None) on success, or (None, reason) when
    nothing is usable. Preference: mp4, then webm, then mov, then
    the other playable containers; within one extension, known
    sizes first, largest first. A file over either cap is skipped
    (and named in the reason when it is the only candidate).
    """
    if not isinstance(files, list):
        return None, "Archive metadata has no file list"
    limit = min(max_file_bytes, budget_bytes)
    playable: list[tuple[int, int, int, dict]] = []
    over_cap: list[tuple[str, int]] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        ext = Path(name).suffix.lower()
        if ext not in VIDEO_EXTENSIONS or ext not in _EXT_PRIORITY:
            continue
        size = _file_size(entry)
        if size is not None and size > limit:
            over_cap.append((name, size))
            continue
        # Sort key: extension priority, then known-size first,
        # then largest first (unknown size sorts last).
        playable.append((_EXT_PRIORITY[ext], 0 if size is not None else 1,
                         -(size or 0), entry))
    if not playable:
        if over_cap:
            biggest = max(over_cap, key=lambda item: item[1])
            return None, (
                f"all {len(over_cap)} video file(s) exceed the size cap "
                f"({limit / 1024 / 1024:.0f} MB): largest is "
                f"{biggest[0]} at {biggest[1] / 1024 / 1024:.1f} MB")
        return None, "no playable video file in the Archive metadata"
    playable.sort(key=lambda item: item[:3])
    return playable[0][3], None


def _http_client(timeout: float, base_url: str) -> httpx.Client:
    """httpx client with the VM egress-proxy handling from
    research/providers.py. Loopback hosts bypass the proxy so a
    local test server is reachable."""
    host = (base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0])
    proxy = None
    if host not in ("localhost", "127.0.0.1", "::1"):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
    bundle = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/certs/ca-certificates.crt"
    if bundle and Path(bundle).exists():
        verify = ssl.create_default_context(cafile=bundle)
    else:
        verify = True
    return httpx.Client(timeout=timeout, trust_env=False, proxy=proxy,
                        verify=verify, follow_redirects=True)


class FootageDownloader:
    """Downloads approved Internet Archive footage to local storage.

    `client_factory` is a test seam: a callable taking a timeout
    and returning an `httpx.Client` (e.g. one built on a
    `MockTransport`). Production always uses the real client.
    """

    def __init__(self, settings: Settings | None = None,
                 client_factory=None):
        self.settings = settings or get_settings()
        self.storage = get_storage(self.settings)
        self._client_factory = client_factory or _http_client

    # ------------------------------------------------------------ main entry
    def download_candidate(self, session, job_id: str, candidate_id: str,
                           budget_bytes: int) -> FootageDownload:
        """Download one approved candidate's source video, or say
        honestly why it could not be downloaded.

        Never raises for a download problem: every failure is a
        `failed`/`skipped` result with a reason, so the caller can
        move on to the next approved candidate. The rights gate
        failure is also a result, never an exception here.
        """
        # RIGHTS GATE FIRST — re-checked server-side, before any
        # network request. Unapproved footage is never downloaded.
        try:
            require_approved(session, job_id, [candidate_id],
                             purpose="footage_download")
        except ValidationError as exc:
            return FootageDownload(candidate_video_id=candidate_id,
                                   status="failed", reason=f"rights gate: {exc}")
        candidate = (session.query(CandidateVideo)
                     .filter(CandidateVideo.id == candidate_id,
                             CandidateVideo.job_id == job_id).first())
        if candidate is None:
            return FootageDownload(candidate_video_id=candidate_id,
                                   status="failed",
                                   reason=f"candidate {candidate_id} not found")
        # An existing local file is reused, never re-downloaded.
        footage = self._footage_row(session, job_id, candidate_id)
        if footage is not None and footage.storage_path:
            existing = Path(footage.storage_path)
            if existing.is_file():
                return FootageDownload(
                    candidate_video_id=candidate_id, status="reused",
                    local_path=str(existing),
                    bytes_downloaded=int(existing.stat().st_size),
                    reason="local file already present")
        source = (session.query(Source)
                  .filter(Source.id == candidate.source_id).first())
        platform = source.platform if source else None
        if platform != "internet_archive":
            return FootageDownload(
                candidate_video_id=candidate_id, status="skipped",
                reason=f"source platform {platform or 'unknown'} has no "
                       "downloader configured — only Internet Archive "
                       "footage is downloaded (YouTube downloads are "
                       "not in scope)")
        identifier = (candidate.external_video_id or "").strip()
        if not identifier:
            return FootageDownload(candidate_video_id=candidate_id,
                                   status="failed",
                                   reason="candidate has no external video id")
        if not self.settings.footage_download_enabled:
            return FootageDownload(
                candidate_video_id=candidate_id, status="skipped",
                reason="footage download is disabled "
                       "(FOOTAGE_DOWNLOAD_ENABLED=false)")
        return self._download_archive(session, job_id, candidate_id,
                                      footage, identifier, budget_bytes)

    # ------------------------------------------------------------ Archive flow
    def _download_archive(self, session, job_id: str, candidate_id: str,
                          footage, identifier: str,
                          budget_bytes: int) -> FootageDownload:
        settings = self.settings
        base = settings.footage_archive_base_url.rstrip("/")
        max_file_bytes = int(settings.footage_max_mb * 1024 * 1024)
        try:
            with self._client_factory(settings.footage_metadata_timeout_seconds,
                                      base) as client:
                response = client.get(f"{base}/metadata/{identifier}")
                response.raise_for_status()
                metadata = response.json()
        except httpx.HTTPStatusError as exc:
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"Archive metadata fetch failed: HTTP "
                       f"{exc.response.status_code} for item {identifier!r}")
        except Exception as exc:  # noqa: BLE001 — network/JSON, reported not raised
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"Archive metadata fetch failed for item "
                       f"{identifier!r}: {exc}")
        entry, reason = select_archive_file(
            metadata.get("files") if isinstance(metadata, dict) else None,
            max_file_bytes, budget_bytes)
        if entry is None:
            return FootageDownload(candidate_video_id=candidate_id,
                                   status="skipped", reason=reason)
        name = str(entry["name"])
        source_url = f"{base}/download/{identifier}/{quote(name, safe='/')}"
        safe_name = Path(name).name
        if not safe_name or safe_name in (".", ".."):
            return FootageDownload(candidate_video_id=candidate_id,
                                   status="failed",
                                   reason=f"unsafe file name in Archive metadata: {name!r}")
        rel = f"jobs/{job_id}/footage/{candidate_id}/{safe_name}"
        try:
            target = safe_join(self.storage.root, rel)
        except ValidationError as exc:
            return FootageDownload(candidate_video_id=candidate_id,
                                   status="failed", reason=str(exc))
        limit = min(max_file_bytes, budget_bytes)
        try:
            written = self._stream_to_file(source_url, target, limit)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self._remove_quietly(target)
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=source_url,
                reason=f"download failed: {exc}")
        # Verify before anything is recorded: a file that does not
        # probe as a video is deleted, never analysed.
        try:
            probe = probe_video(target, ffprobe_path=settings.ffprobe_path)
        except (StorageError, ValidationError) as exc:
            self._remove_quietly(target)
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=source_url,
                reason=f"downloaded file failed ffprobe verification: {exc}")
        footage = self._record_footage(session, job_id, candidate_id,
                                       footage, str(target), probe.duration_seconds)
        session.flush()
        return FootageDownload(
            candidate_video_id=candidate_id, status="downloaded",
            source_url=source_url, local_path=str(target),
            bytes_downloaded=written,
            reason=f"downloaded {safe_name} "
                   f"({written / 1024 / 1024:.1f} MB, "
                   f"{probe.duration_seconds:.1f}s)")

    def _stream_to_file(self, url: str, target: Path, limit_bytes: int) -> int:
        """Stream a URL to `target`, aborting over the byte limit.

        Returns the bytes written. Raises httpx.HTTPError on a bad
        status, ValueError when the limit is exceeded, OSError on a
        write failure. A partial file is the caller's to remove.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with self._client_factory(self.settings.footage_download_timeout_seconds,
                                  url) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                declared_bytes: int | None = None
                try:
                    declared_bytes = int(declared) if declared is not None else None
                except (TypeError, ValueError):
                    declared_bytes = None
                if declared_bytes is not None and declared_bytes > limit_bytes:
                    raise ValueError(
                        f"file is {declared_bytes / 1024 / 1024:.1f} MB, "
                        f"over the {limit_bytes / 1024 / 1024:.0f} MB limit")
                with target.open("wb") as fh:
                    for chunk in response.iter_bytes(_CHUNK_BYTES):
                        written += len(chunk)
                        if written > limit_bytes:
                            raise ValueError(
                                f"download exceeded the "
                                f"{limit_bytes / 1024 / 1024:.0f} MB limit "
                                f"after {written / 1024 / 1024:.1f} MB")
                        fh.write(chunk)
        return written

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _remove_quietly(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _footage_row(session, job_id: str, candidate_id: str):
        return (session.query(ApprovedFootage)
                .filter(ApprovedFootage.candidate_video_id == candidate_id).first())

    def _record_footage(self, session, job_id: str, candidate_id: str,
                        footage, storage_path: str, duration: float):
        """Record the downloaded file on the ApprovedFootage row,
        creating the row (from the latest rights evidence) when
        Rights never wrote one."""
        if footage is None:
            evidence = latest_evidence(session, candidate_id)
            footage = ApprovedFootage(
                job_id=job_id, candidate_video_id=candidate_id,
                rights_evidence_id=evidence.id if evidence else None,
                storage_path=storage_path, duration_seconds=duration)
            session.add(footage)
        else:
            footage.storage_path = storage_path
            footage.duration_seconds = duration
        return footage
