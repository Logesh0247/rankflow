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
* Live sources are Internet Archive and YouTube. Archive: given an
  item identifier (the candidate's `external_video_id`), fetch
  `{base}/metadata/{id}`, pick a playable video file (mp4 first,
  then webm, mov, …), stream it to disk and verify it with
  ffprobe (duration > 0) before it is ever analysed. YouTube: an
  approved CC-licensed video is downloaded with Vid Kraken first
  when `VIDKRAKEN_API_KEY` is configured (POST /api/v2/download,
  poll until COMPLETED, stream the returned downloadUrl — no
  cookies/proxies, which fixes the Render sign-in block), and
  with yt-dlp otherwise (mp4, `--max-filesize` enforces the
  per-file cap), verified the same way. YouTube demands a
  signed-in browser from some cloud servers (live failure
  2026-11-21: every download failed with "--from-browser or
  --cookies for the authentication"); when yt-dlp fails that way
  the download is retried with the mobile player clients
  (`--extractor-args youtube:player_client=android`, then `ios`),
  which need no account and no cookies and often bypass the
  check. Which player client succeeded is recorded on the
  download outcome (`FootageDownload.player_client`); a Vid
  Kraken download records its job id (`vidkraken_job_id`). No
  cookies support exists and no account is ever used. Pexels /
  Pixabay: approved stock clips are fetched
  by re-querying the provider API for the video id (the search
  response carries no file link), the direct MP4 link is selected
  (Pexels: largest mp4 up to 1920 px wide; Pixabay: medium 1080p
  tier first), streamed with the same caps and verified the same
  way. A file that fails to download or probe is deleted, never
  analysed, never presented as if it existed.
* RIGHTS GATE: download ONLY footage with an approved Rights
  record. The gate is re-checked server-side
  (`require_approved`) before every download. A rejected or
  needs-review candidate is never downloaded — a failed gate
  returns a `failed` result, it never raises past the caller.
  For YouTube this is also what enforces "CC only": Rights
  approves a YouTube video only on a Creative Commons licence
  (CC BY 3.0) or a recorded human permission grant, so a
  standard-licence video can never reach the downloader.
* NO TRANSACTION SPANS A DOWNLOAD (2026-11-21 lock fix): the
  streaming download / yt-dlp run happens with no database
  session open at all — the gate and lookups are reads done
  first, then the file is fetched, and only afterwards a short
  session records `storage_path` + duration and commits
  immediately. A long download therefore never holds a write
  lock that blocks other jobs (the live incident: the database
  locked while an Archive download streamed, and every other
  write — new jobs, cancels, even recording the failure —
  failed with "database is locked"). SQLite WAL mode + a 30 s
  busy timeout (see `app/database/session.py`) is the other
  half of that fix.
* Fail soft per file: one candidate failing to download marks
  that footage unusable and the stage moves on to the next
  approved candidate. Only when no approved candidate could be
  downloaded does the stage fail — honestly, naming counts.
* Bounds for the Render free plan: a per-file size cap and a
  per-job total cap (both configurable, see `Settings`). Files
  over the cap are skipped with a clear reason; the caps are
  enforced while streaming (Archive) or by yt-dlp's
  `--max-filesize` (YouTube), so an oversized file is aborted
  instead of filling the disk.
* A platform with no downloader (TikTok, Facebook, Instagram,
  Pinterest — none of them offers an official search/download
  API) is skipped with a reason saying so. Pexels and Pixabay
  ARE downloaded (direct MP4 links from their APIs), as are
  Vimeo (CC only — the video record is re-fetched by id and
  the MP4 download/file link selected) and Wikimedia Commons
  (the direct file URL from the Commons API imageinfo).

The HTTP client mirrors the egress-proxy handling in
`app/agents/research/providers.py` (trust_env=False, proxy from
the environment, TLS verified — never disabled), except it
bypasses the proxy for loopback hosts so tests can serve
metadata and files from a local test server via
`footage_archive_base_url`. yt-dlp is run as a subprocess with
TLS verification left on: it verifies against the system CA
bundle, as on the deploy host. It is never run with certificate
checks disabled.
"""
from __future__ import annotations

import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel
from sqlalchemy.orm import sessionmaker

from app.agents.research.providers import (
    VIDEO_EXTENSIONS,
    select_pexels_file,
    select_pixabay_file,
    select_vimeo_file,
)
from app.agents.rights.gate import latest_evidence, require_approved
from app.agents.video_intelligence.probe import probe_video
from app.core.config import Settings, get_settings
from app.core.errors import StorageError, ValidationError
from app.core.security import safe_join
from app.database.session import make_engine
from app.models.models import ApprovedFootage, CandidateVideo, Source
from app.storage.local import get_storage

# Extension preference order for Archive files: mp4 first, then
# webm, then mov, then the rest of the playable containers.
_EXT_PRIORITY = {".mp4": 0, ".webm": 1, ".mov": 2, ".mkv": 3,
                 ".m4v": 4, ".ogv": 5, ".avi": 6}

_CHUNK_BYTES = 1024 * 1024  # stream in 1 MB chunks

# YouTube player clients, tried in order when a download fails.
# "web" is the default yt-dlp client (no extractor args, exactly
# the command used before the fallback existed). YouTube demands
# a signed-in browser from some cloud servers; the mobile player
# clients ("android", then "ios") often bypass that check, and
# they need no account and no cookies. No cookies support exists
# anywhere in this module — no account is ever used.
_YT_PLAYER_CLIENTS = ("web", "android", "ios")

# Vid Kraken (managed YouTube download API, added 2026-11-21).
# Auth is `Authorization: Bearer <key>` (vidkraken.com/llms.txt).
# Workflow: POST /api/v2/download {"url", "format": "1080"} ->
# {"jobId", ...}, then poll GET /api/v2/download/{jobId} until
# status COMPLETED (IN_QUEUE / IN_PROGRESS in between) and stream
# the returned downloadUrl. A FAILED job carries a machine-readable
# errorCode (AGE_RESTRICTED, MEMBERS_ONLY, GEO_RESTRICTED,
# PRIVATE_VIDEO, DRM_PROTECTED, VIDEO_UNAVAILABLE, VIDEO_NOT_FOUND,
# ...). Only NETWORK_ERROR is worth resubmitting; the rest are
# definitive rejections of that video. The API key is sent only to
# vidkraken.com, never to the file host (the file is streamed with
# a fresh unauthenticated client). Use only with content the user
# has the right to download — Vid Kraken never bypasses Rights.
VIDKRAKEN_BASE_URL = "https://vidkraken.com"
VIDKRAKEN_FORMAT = "1080"
# Definitive Vid Kraken job failures: the video itself cannot be
# downloaded, so yt-dlp must not be tried to get around them.
_VIDKRAKEN_DEFINITIVE_ERRORS = frozenset({
    "AGE_RESTRICTED", "MEMBERS_ONLY", "GEO_RESTRICTED",
    "PRIVATE_VIDEO", "DRM_PROTECTED", "VIDEO_UNAVAILABLE",
    "VIDEO_NOT_FOUND", "VIDEO_PROCESSING", "VIDEO_PREMIERE",
    "LIVE_STREAM_IN_PROGRESS", "NOT_FOUND",
})

# Substrings (lowercased) of yt-dlp stderr that mean "YouTube is
# demanding a signed-in browser" — the error seen live on
# 2026-11-21 ("--from-browser or --cookies for the
# authentication", "Sign in to confirm you're not a bot"). Only
# these trigger the player-client fallback; any other failure
# (video unavailable, over the size cap, network error) does not.
_YT_AUTH_ERROR_MARKERS = (
    "sign in to confirm",
    "confirm your age",
    "--cookies",
    "from-browser",
    "cookies for the authentication",
    "login required",
)


def _is_youtube_auth_error(stderr: str) -> bool:
    """True when yt-dlp failed because YouTube demanded a signed-in
    browser, as opposed to any other failure."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _YT_AUTH_ERROR_MARKERS)


def _part_path(target: Path) -> Path:
    """yt-dlp's partial-download file for a target output path."""
    return target.with_name(target.name + ".part")


def _remove_partials(target: Path, video_id: str) -> None:
    """Remove a failed attempt's partial output: the target itself,
    its `.part` file, and any `video_id.*.part` fragments yt-dlp
    left in the same directory, so a retry starts clean."""
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _part_path(target).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        for fragment in target.parent.glob(f"{video_id}.*.part"):
            try:
                fragment.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


class FootageDownload(BaseModel):
    """The outcome of one footage-download attempt."""

    candidate_video_id: str
    status: str  # "downloaded" | "reused" | "skipped" | "failed"
    source_url: str | None = None
    local_path: str | None = None
    bytes_downloaded: int = 0
    duration_seconds: float | None = None
    reason: str | None = None
    # YouTube only: which yt-dlp player client produced the file
    # ("web" | "android" | "ios"), so a later job's diagnostics
    # show whether the sign-in fallback was needed. None for
    # non-YouTube downloads and for failed/skipped attempts.
    player_client: str | None = None
    # YouTube only: the Vid Kraken job id that produced the file,
    # when Vid Kraken was the downloader. None otherwise.
    vidkraken_job_id: str | None = None


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


def _run_ytdlp(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run yt-dlp as a subprocess, capturing output.

    TLS verification is left on (yt-dlp verifies against the
    system CA bundle); it is never run with certificate checks
    disabled. A timeout raises subprocess.TimeoutExpired.
    """
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class FootageDownloader:
    """Downloads approved Internet Archive, YouTube and stock
    (Pexels / Pixabay) footage to local storage.

    `client_factory` is a test seam: a callable taking a timeout
    and returning an `httpx.Client` (e.g. one built on a
    `MockTransport`). `ytdlp_runner` is the test seam for the
    YouTube download: a callable taking (cmd, timeout) and
    returning an object with `returncode`, `stdout` and `stderr`
    (like `subprocess.CompletedProcess`). `session_factory` is
    the test seam for the short record session: a callable
    returning a new SQLAlchemy session. Production always uses
    the real client, the real yt-dlp binary, and a session from
    its own engine (same database file — WAL mode lets it write
    while other connections read).
    """

    def __init__(self, settings: Settings | None = None,
                 client_factory=None, ytdlp_runner=None,
                 session_factory=None):
        self.settings = settings or get_settings()
        self.storage = get_storage(self.settings)
        self._client_factory = client_factory or _http_client
        self._ytdlp_runner = ytdlp_runner or _run_ytdlp
        self._session_factory = session_factory
        self._engine = None

    def _new_session(self):
        """A fresh short-lived session, used ONLY to record a
        completed download (open, write, commit, close). It is
        never held open across a download."""
        if self._session_factory is None:
            if self._engine is None:
                self._engine = make_engine(self.settings)
            self._session_factory = sessionmaker(bind=self._engine)
        return self._session_factory()

    # ------------------------------------------------------------ main entry
    def download_candidate(self, session, job_id: str, candidate_id: str,
                           budget_bytes: int) -> FootageDownload:
        """Download one approved candidate's source video, or say
        honestly why it could not be downloaded.

        Transaction discipline (2026-11-21 lock fix): the `session`
        passed in is used ONLY for reads (rights gate, candidate
        and footage lookups). The long download itself — streaming
        an Archive file or running yt-dlp — happens with no
        database session open at all. Only after the file is on
        disk and verified does a short session record
        `storage_path` + duration and commit immediately. A
        download therefore never holds a transaction open while
        it streams.

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
        if not self.settings.footage_download_enabled:
            return FootageDownload(
                candidate_video_id=candidate_id, status="skipped",
                reason="footage download is disabled "
                       "(FOOTAGE_DOWNLOAD_ENABLED=false)")
        if platform == "internet_archive":
            identifier = (candidate.external_video_id or "").strip()
            if not identifier:
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    reason="candidate has no external video id")
            result = self._download_archive(job_id, candidate_id,
                                            identifier, budget_bytes)
        elif platform == "youtube":
            video_id = (candidate.external_video_id or "").strip()
            if not video_id:
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    reason="candidate has no external video id")
            result = self._download_youtube(job_id, candidate_id,
                                            video_id, budget_bytes)
        elif platform in ("pexels", "pixabay"):
            external_id = (candidate.external_video_id or "").strip()
            if not external_id:
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    reason="candidate has no external video id")
            result = self._download_stock(job_id, candidate_id,
                                          platform, external_id,
                                          budget_bytes)
        elif platform in ("vimeo", "wikimedia_commons"):
            external_id = (candidate.external_video_id or "").strip()
            if not external_id:
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    reason="candidate has no external video id")
            if platform == "vimeo":
                result = self._download_vimeo(job_id, candidate_id,
                                              external_id, budget_bytes)
            else:
                result = self._download_wikimedia(job_id, candidate_id,
                                                  external_id, budget_bytes)
        else:
            return FootageDownload(
                candidate_video_id=candidate_id, status="skipped",
                reason=f"source platform {platform or 'unknown'} has no "
                       "downloader configured — only Internet Archive, "
                       "YouTube (Creative Commons), Pexels, Pixabay, "
                       "Vimeo (Creative Commons) and Wikimedia Commons "
                       "footage is downloaded; TikTok, Facebook, "
                       "Instagram and Pinterest offer no official "
                       "search/download API")
        if result.status != "downloaded" or not result.local_path:
            return result
        # The file is downloaded and verified. Record it in a
        # short session: open, write, commit, close — never held
        # open across the download that just finished.
        recorded = self._record_download(
            job_id, candidate_id, result.local_path,
            result.duration_seconds)
        if not recorded:
            self._remove_quietly(Path(result.local_path))
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=result.source_url,
                reason="downloaded file could not be recorded in the database")
        return result

    def _record_download(self, job_id: str, candidate_id: str,
                         local_path: str, duration: float | None) -> bool:
        """Record a verified download on the ApprovedFootage row in
        a short session. Returns False (after rollback) when the
        record could not be written — the caller deletes the file."""
        try:
            with self._new_session() as session:
                footage = self._footage_row(session, job_id, candidate_id)
                self._record_footage(session, job_id, candidate_id, footage,
                                     local_path, duration or 0.0)
                session.commit()
            return True
        except Exception:  # noqa: BLE001 — a failed record is a failed download
            return False

    # ------------------------------------------------------------ Archive flow
    def _download_archive(self, job_id: str, candidate_id: str,
                          identifier: str,
                          budget_bytes: int) -> FootageDownload:
        """Fetch an Archive item's video file to local storage.

        No database session is open during this call: the file is
        streamed and verified first; the caller records it
        afterwards in a short session. `job_id` / `candidate_id`
        are plain strings used only to build the storage path."""
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
        return FootageDownload(
            candidate_video_id=candidate_id, status="downloaded",
            source_url=source_url, local_path=str(target),
            bytes_downloaded=written,
            duration_seconds=probe.duration_seconds,
            reason=f"downloaded {safe_name} "
                   f"({written / 1024 / 1024:.1f} MB, "
                   f"{probe.duration_seconds:.1f}s)")

    # ------------------------------------------------------------ YouTube flow
    def _resolve_ytdlp(self) -> list[str] | None:
        """Command prefix that runs yt-dlp, or None when it is not
        installed. Prefers the configured binary on PATH, then
        `python -m yt_dlp` when the package is importable."""
        binary = shutil.which(self.settings.footage_ytdlp_path)
        if binary:
            return [binary]
        import importlib.util
        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
        return None

    def _run_ytdlp_attempt(self, prefix: list[str], url: str,
                           target: Path, limit: int,
                           player_client: str) -> subprocess.CompletedProcess:
        """One yt-dlp run for a YouTube video, with the given player
        client. "web" is the default (no extractor args — exactly
        the command used before the fallback existed); "android"
        / "ios" add `--extractor-args
        youtube:player_client=<client>`. Each attempt is bounded
        by FOOTAGE_DOWNLOAD_TIMEOUT_SECONDS."""
        cmd = prefix + [
            "--no-warnings", "--no-playlist",
            "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--max-filesize", str(limit),
            "--output", str(target), url]
        if player_client != "web":
            cmd = prefix + [
                "--no-warnings", "--no-playlist",
                "--extractor-args",
                f"youtube:player_client={player_client}",
                "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "--max-filesize", str(limit),
                "--output", str(target), url]
        return self._ytdlp_runner(cmd,
                                  self.settings.footage_download_timeout_seconds)

    # ------------------------------------------------------------ Vid Kraken (YouTube only)
    def _vidkraken_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.vidkraken_api_key}",
                "Accept": "application/json"}

    def _try_vidkraken(self, job_id: str, candidate_id: str,
                       video_id: str,
                       budget_bytes: int) -> FootageDownload | None:
        """Try Vid Kraken for an approved YouTube video.

        Returns None when Vid Kraken was NOT attempted or its
        service failed in a way yt-dlp may still succeed — no
        key configured, submit HTTP/network error, poll HTTP
        error, or poll timeout — and the caller then falls back
        to yt-dlp. Returns a FootageDownload for a definitive
        outcome: downloaded, or the Vid Kraken job FAILED with a
        machine-readable errorCode (AGE_RESTRICTED, MEMBERS_ONLY,
        GEO_RESTRICTED, PRIVATE_VIDEO, DRM_PROTECTED,
        VIDEO_UNAVAILABLE, …). A definitive failure is never
        retried with yt-dlp: yt-dlp cannot download an
        age-restricted / private / members-only video either,
        and trying would be an attempt to get around the
        restriction. No database session is open during this
        call: the API is polled and the file streamed first; the
        caller records it afterwards in a short session. The
        API key is sent only to vidkraken.com, never to the file
        host. The submit carries an Idempotency-Key so a
        retried submit cannot start (and bill) a second job.
        """
        if not self.settings.vidkraken_api_key:
            return None
        url = f"https://www.youtube.com/watch?v={video_id}"
        submit_url = f"{VIDKRAKEN_BASE_URL}/api/v2/download"
        headers = dict(self._vidkraken_headers())
        headers["Idempotency-Key"] = uuid.uuid4().hex
        try:
            with self._client_factory(
                    self.settings.footage_metadata_timeout_seconds,
                    submit_url) as client:
                response = client.post(
                    submit_url,
                    json={"url": url, "format": VIDKRAKEN_FORMAT},
                    headers=headers)
                response.raise_for_status()
                submitted = response.json()
        except Exception:  # noqa: BLE001 — service failure: fall back to yt-dlp
            return None
        if not isinstance(submitted, dict):
            return None
        job_id_vk = str(submitted.get("jobId") or submitted.get("job_id") or "").strip()
        # A submit that already reports COMPLETED carries the link.
        status = str(submitted.get("status") or "").upper()
        if status == "COMPLETED":
            download_url = str(submitted.get("downloadUrl")
                               or submitted.get("download_url") or "").strip()
            if not download_url:
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    source_url=url, vidkraken_job_id=job_id_vk or None,
                    reason="Vid Kraken reported COMPLETED but returned "
                           "no downloadUrl")
            return self._vidkraken_stream(job_id, candidate_id, video_id,
                                          download_url, job_id_vk,
                                          budget_bytes)
        if not job_id_vk:
            return None
        deadline = time.monotonic() + max(
            0.0, float(self.settings.vidkraken_poll_timeout_seconds))
        poll_url = f"{VIDKRAKEN_BASE_URL}/api/v2/download/{job_id_vk}"
        while True:
            try:
                with self._client_factory(
                        self.settings.footage_metadata_timeout_seconds,
                        poll_url) as client:
                    response = client.get(poll_url,
                                          headers=self._vidkraken_headers())
                    response.raise_for_status()
                    state = response.json()
            except Exception:  # noqa: BLE001 — poll failure: fall back to yt-dlp
                return None
            if not isinstance(state, dict):
                return None
            status = str(state.get("status") or "").upper()
            if status == "COMPLETED":
                download_url = str(state.get("downloadUrl")
                                   or state.get("download_url") or "").strip()
                if not download_url:
                    return FootageDownload(
                        candidate_video_id=candidate_id, status="failed",
                        source_url=url, vidkraken_job_id=job_id_vk,
                        reason="Vid Kraken reported COMPLETED but returned "
                               "no downloadUrl")
                return self._vidkraken_stream(job_id, candidate_id, video_id,
                                              download_url, job_id_vk,
                                              budget_bytes)
            if status == "FAILED":
                error_code = str(state.get("errorCode")
                                 or state.get("error_code") or "FAILED")
                message = str(state.get("message") or state.get("error") or "")
                reason = f"Vid Kraken download failed: {error_code}"
                if message and message != error_code:
                    reason += f" — {message}"
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    source_url=url, vidkraken_job_id=job_id_vk,
                    reason=reason)
            if status not in ("IN_QUEUE", "IN_PROGRESS", "QUEUED", "PROCESSING", ""):
                # An unknown terminal-looking status: do not guess —
                # treat it as a definitive failure, not a success.
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    source_url=url, vidkraken_job_id=job_id_vk,
                    reason=f"Vid Kraken download ended with unexpected "
                           f"status {status!r}")
            if time.monotonic() >= deadline:
                return None  # poll timeout: fall back to yt-dlp
            interval = max(0.0, float(self.settings.vidkraken_poll_interval_seconds))
            if interval:
                time.sleep(interval)

    def _vidkraken_stream(self, job_id: str, candidate_id: str,
                          video_id: str, download_url: str,
                          job_id_vk: str,
                          budget_bytes: int) -> FootageDownload:
        """Stream a Vid Kraken COMPLETED downloadUrl to local storage."""
        base_name = Path(urlparse(download_url).path).name
        if Path(base_name).suffix.lower() not in VIDEO_EXTENSIONS:
            base_name = f"{video_id}.mp4"
        result = self._download_direct_file(job_id, candidate_id,
                                            download_url, base_name,
                                            budget_bytes)
        if result.status != "downloaded":
            return FootageDownload(
                candidate_video_id=result.candidate_video_id,
                status=result.status, source_url=result.source_url,
                local_path=result.local_path,
                bytes_downloaded=result.bytes_downloaded,
                duration_seconds=result.duration_seconds,
                reason=result.reason, vidkraken_job_id=job_id_vk)
        return FootageDownload(
            candidate_video_id=result.candidate_video_id,
            status="downloaded", source_url=result.source_url,
            local_path=result.local_path,
            bytes_downloaded=result.bytes_downloaded,
            duration_seconds=result.duration_seconds,
            reason=f"{result.reason} via Vid Kraken",
            vidkraken_job_id=job_id_vk)

    def _download_youtube(self, job_id: str, candidate_id: str,
                          video_id: str,
                          budget_bytes: int) -> FootageDownload:
        """Download an approved CC-licensed YouTube video with yt-dlp.

        No database session is open during this call: yt-dlp runs
        and the file is verified first; the caller records it
        afterwards in a short session. `job_id` / `candidate_id`
        are plain strings used only to build the storage path.

        The Rights gate was already re-checked in
        `download_candidate`, so reaching here means Rights
        approved this video — on a Creative Commons licence or a
        recorded human permission grant. A standard-licence video
        can never reach this method. The video id is validated
        (YouTube ids are 11 chars of [A-Za-z0-9_-]) so no
        attacker-controlled string reaches the command line.

        Sign-in fallback: YouTube demands a signed-in browser
        from some cloud servers, and yt-dlp then fails with an
        authentication error ("--from-browser or --cookies for
        the authentication"). When that happens the same download
        is retried with the mobile player clients — android,
        then ios — which need no account and no cookies and
        often bypass the check. Any other failure (video
        unavailable, over the size cap, timeout, yt-dlp missing)
        returns immediately without a retry. The player client
        that produced the file is recorded on the outcome
        (`player_client`), and named in the reason when a
        fallback client was needed.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", video_id):
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"unsafe YouTube video id: {video_id!r}")
        limit = min(int(self.settings.footage_max_mb * 1024 * 1024),
                    budget_bytes)
        if limit <= 0:
            return FootageDownload(
                candidate_video_id=candidate_id, status="skipped",
                reason="no download budget left for this job "
                       f"(per-job cap {self.settings.footage_total_max_mb:.0f} MB)")
        # Vid Kraken first, when a key is configured: it downloads
        # on Vid Kraken's servers with no cookies/proxies, so it
        # fixes the Render yt-dlp sign-in block. A definitive Vid
        # Kraken failure (job FAILED with an errorCode) is returned
        # directly, never retried with yt-dlp. When Vid Kraken is
        # not configured, or its service fails / times out, fall
        # through to yt-dlp below.
        vidkraken_result = self._try_vidkraken(job_id, candidate_id,
                                               video_id, budget_bytes)
        if vidkraken_result is not None:
            return vidkraken_result
        prefix = self._resolve_ytdlp()
        if prefix is None:
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason="yt-dlp is not installed — install it "
                       "(pip install yt-dlp) or set FOOTAGE_YTDLP_PATH")
        url = f"https://www.youtube.com/watch?v={video_id}"
        rel = f"jobs/{job_id}/footage/{candidate_id}/{video_id}.mp4"
        try:
            target = safe_join(self.storage.root, rel)
        except ValidationError as exc:
            return FootageDownload(candidate_video_id=candidate_id,
                                   status="failed", reason=str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        player_client = "web"
        for client in _YT_PLAYER_CLIENTS:
            player_client = client
            try:
                completed = self._run_ytdlp_attempt(
                    prefix, url, target, limit, client)
            except subprocess.TimeoutExpired:
                _remove_partials(target, video_id)
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    source_url=url,
                    reason="YouTube download timed out after "
                           f"{self.settings.footage_download_timeout_seconds:.0f}s")
            except OSError as exc:
                _remove_partials(target, video_id)
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    source_url=url, reason=f"could not run yt-dlp: {exc}")
            stderr = (getattr(completed, "stderr", "") or "").strip()
            if completed.returncode == 0:
                break
            # A failed attempt never leaves a partial file behind.
            _remove_partials(target, video_id)
            if "larger than max-filesize" in stderr:
                return FootageDownload(
                    candidate_video_id=candidate_id, status="skipped",
                    source_url=url,
                    reason=f"YouTube video exceeds the size cap "
                           f"({limit / 1024 / 1024:.0f} MB)")
            if _is_youtube_auth_error(stderr) and client != "ios":
                # YouTube demanded a signed-in browser from this
                # server; retry with the next mobile player
                # client, which needs no account and no cookies.
                continue
            detail = stderr[-300:] if stderr else f"exit code {completed.returncode}"
            reason = f"yt-dlp failed: {detail}"
            if _is_youtube_auth_error(stderr):
                reason += (" — YouTube required sign-in from this "
                           "server even after trying the android/ios "
                           "player clients")
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=url, reason=reason)
        # yt-dlp reports success; the file must actually be there.
        if not target.is_file():
            found = sorted(target.parent.glob(f"{video_id}.*"))
            if found:
                target = found[0]
            else:
                return FootageDownload(
                    candidate_video_id=candidate_id, status="failed",
                    source_url=url,
                    reason="yt-dlp finished but produced no file")
        written = int(target.stat().st_size)
        if written > limit:
            # yt-dlp's --max-filesize is checked before the
            # download; a merged file can still end up larger.
            self._remove_quietly(target)
            return FootageDownload(
                candidate_video_id=candidate_id, status="skipped",
                source_url=url,
                reason=f"YouTube video exceeds the size cap "
                       f"({limit / 1024 / 1024:.0f} MB)")
        # Verify before anything is recorded: a file that does not
        # probe as a video is deleted, never analysed.
        try:
            probe = probe_video(target, ffprobe_path=self.settings.ffprobe_path)
        except (StorageError, ValidationError) as exc:
            self._remove_quietly(target)
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=url,
                reason=f"downloaded file failed ffprobe verification: {exc}")
        return FootageDownload(
            candidate_video_id=candidate_id, status="downloaded",
            source_url=url, local_path=str(target),
            bytes_downloaded=written,
            duration_seconds=probe.duration_seconds,
            player_client=player_client,
            reason=f"downloaded {video_id}.mp4 "
                   f"({written / 1024 / 1024:.1f} MB, "
                   f"{probe.duration_seconds:.1f}s)"
                   + (f" via {player_client} player client"
                      if player_client != "web" else ""))

    # ------------------------------------------------------------ stock flow (Pexels / Pixabay)
    def _download_stock(self, job_id: str, candidate_id: str,
                        platform: str, external_id: str,
                        budget_bytes: int) -> FootageDownload:
        """Download an approved Pexels / Pixabay stock clip.

        No database session is open during this call: the provider
        API is queried, the file is streamed and verified first;
        the caller records it afterwards in a short session.

        The search response carries no file link, so the video is
        re-fetched by id from the provider API (Pexels "get video
        by id", Pixabay `id` param) — the same re-fetch pattern as
        the Archive metadata lookup — and the direct MP4 link is
        selected from that response. The Rights gate was already
        re-checked in `download_candidate`, so reaching here means
        Rights approved this clip on its platform licence (Pexels
        License / Pixabay Content License).
        """
        settings = self.settings
        api_key = (settings.pexels_api_key if platform == "pexels"
                   else settings.pixabay_api_key)
        if not api_key:
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"{platform} API key is not configured — the clip "
                       f"cannot be re-fetched for download")
        limit = min(int(settings.footage_max_mb * 1024 * 1024),
                    budget_bytes)
        if limit <= 0:
            return FootageDownload(
                candidate_video_id=candidate_id, status="skipped",
                reason="no download budget left for this job "
                       f"(per-job cap {settings.footage_total_max_mb:.0f} MB)")
        if platform == "pexels":
            detail_url = f"https://api.pexels.com/videos/videos/{external_id}"
            headers = {"Authorization": api_key}
            params = None
        else:
            detail_url = "https://pixabay.com/api/videos/"
            headers = None
            params = {"key": api_key, "id": external_id}
        try:
            with self._client_factory(settings.footage_metadata_timeout_seconds,
                                      detail_url) as client:
                response = client.get(detail_url, headers=headers,
                                      params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"{platform} video lookup failed: HTTP "
                       f"{exc.response.status_code} for id {external_id!r}")
        except Exception as exc:  # noqa: BLE001 — network/JSON, reported not raised
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"{platform} video lookup failed for id "
                       f"{external_id!r}: {exc}")
        if platform == "pexels":
            file_url = select_pexels_file(
                data.get("video_files") if isinstance(data, dict) else None)
        else:
            hits = data.get("hits") if isinstance(data, dict) else None
            hit = hits[0] if isinstance(hits, list) and hits else None
            file_url = select_pixabay_file(
                hit.get("videos") if isinstance(hit, dict) else None)
        if not file_url:
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"{platform} returned no downloadable MP4 file "
                       f"for id {external_id!r}")
        # Safe file name from the link itself; fall back to the id.
        base_name = Path(urlparse(file_url).path).name
        if Path(base_name).suffix.lower() not in VIDEO_EXTENSIONS:
            base_name = f"{external_id}.mp4"
        if not base_name or base_name in (".", ".."):
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"unsafe file name in {platform} file link: {file_url!r}")
        rel = f"jobs/{job_id}/footage/{candidate_id}/{base_name}"
        try:
            target = safe_join(self.storage.root, rel)
        except ValidationError as exc:
            return FootageDownload(candidate_video_id=candidate_id,
                                   status="failed", reason=str(exc))
        try:
            written = self._stream_to_file(file_url, target, limit)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self._remove_quietly(target)
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=file_url,
                reason=f"download failed: {exc}")
        # Verify before anything is recorded: a file that does not
        # probe as a video is deleted, never analysed.
        try:
            probe = probe_video(target, ffprobe_path=settings.ffprobe_path)
        except (StorageError, ValidationError) as exc:
            self._remove_quietly(target)
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=file_url,
                reason=f"downloaded file failed ffprobe verification: {exc}")
        return FootageDownload(
            candidate_video_id=candidate_id, status="downloaded",
            source_url=file_url, local_path=str(target),
            bytes_downloaded=written,
            duration_seconds=probe.duration_seconds,
            reason=f"downloaded {base_name} "
                   f"({written / 1024 / 1024:.1f} MB, "
                   f"{probe.duration_seconds:.1f}s)")

    # ------------------------------------------------------------ Vimeo / Wikimedia flow
    def _download_direct_file(self, job_id: str, candidate_id: str,
                              source_url: str, file_name: str,
                              budget_bytes: int) -> FootageDownload:
        """Stream a direct file URL to local storage and verify it.

        Shared by the Vimeo and Wikimedia downloaders. No database
        session is open during this call: the file is streamed and
        verified first; the caller records it afterwards in a short
        session. The per-file / per-job caps are enforced while
        streaming, exactly as for Archive / stock downloads.
        """
        settings = self.settings
        limit = min(int(settings.footage_max_mb * 1024 * 1024),
                    budget_bytes)
        if limit <= 0:
            return FootageDownload(
                candidate_video_id=candidate_id, status="skipped",
                source_url=source_url,
                reason="no download budget left for this job "
                       f"(per-job cap {settings.footage_total_max_mb:.0f} MB)")
        if not file_name or file_name in (".", ".."):
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=source_url,
                reason=f"unsafe file name in download link: {source_url!r}")
        rel = f"jobs/{job_id}/footage/{candidate_id}/{file_name}"
        try:
            target = safe_join(self.storage.root, rel)
        except ValidationError as exc:
            return FootageDownload(candidate_video_id=candidate_id,
                                   status="failed", source_url=source_url,
                                   reason=str(exc))
        try:
            written = self._stream_to_file(source_url, target, limit)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self._remove_quietly(target)
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=source_url,
                reason=f"download failed: {exc}")
        try:
            probe = probe_video(target, ffprobe_path=settings.ffprobe_path)
        except (StorageError, ValidationError) as exc:
            self._remove_quietly(target)
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                source_url=source_url,
                reason=f"downloaded file failed ffprobe verification: {exc}")
        return FootageDownload(
            candidate_video_id=candidate_id, status="downloaded",
            source_url=source_url, local_path=str(target),
            bytes_downloaded=written,
            duration_seconds=probe.duration_seconds,
            reason=f"downloaded {file_name} "
                   f"({written / 1024 / 1024:.1f} MB, "
                   f"{probe.duration_seconds:.1f}s)")

    def _download_vimeo(self, job_id: str, candidate_id: str,
                        video_id: str,
                        budget_bytes: int) -> FootageDownload:
        """Download an approved CC Vimeo video via its API file link.

        The search response's download link is a time-limited
        redirect, so the video record is re-fetched by id from
        the Vimeo API (the same re-fetch pattern as Pexels /
        Pixabay) and the direct MP4 link is selected from that
        fresh record. The Rights gate was already re-checked in
        `download_candidate`, so reaching here means Rights
        approved this video on a Creative Commons licence.
        """
        settings = self.settings
        token = settings.vimeo_access_token
        if not token:
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason="VIMEO_ACCESS_TOKEN is not configured — the clip "
                       "cannot be re-fetched for download")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", video_id):
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"unsafe Vimeo video id: {video_id!r}")
        detail_url = f"https://api.vimeo.com/videos/{video_id}"
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/vnd.vimeo.*+json;version=3.4"}
        try:
            with self._client_factory(settings.footage_metadata_timeout_seconds,
                                      detail_url) as client:
                response = client.get(detail_url, headers=headers,
                                      params={"fields": "download,files"})
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"vimeo video lookup failed: HTTP "
                       f"{exc.response.status_code} for id {video_id!r}")
        except Exception as exc:  # noqa: BLE001 — network/JSON, reported not raised
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"vimeo video lookup failed for id "
                       f"{video_id!r}: {exc}")
        file_url = select_vimeo_file(data if isinstance(data, dict) else None)
        if not file_url:
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"vimeo returned no downloadable MP4 file "
                       f"for id {video_id!r}")
        base_name = Path(urlparse(file_url).path).name
        if Path(base_name).suffix.lower() not in VIDEO_EXTENSIONS:
            base_name = f"{video_id}.mp4"
        return self._download_direct_file(job_id, candidate_id, file_url,
                                          base_name, budget_bytes)

    def _download_wikimedia(self, job_id: str, candidate_id: str,
                            file_title: str,
                            budget_bytes: int) -> FootageDownload:
        """Download an approved Wikimedia Commons video file.

        The file is re-fetched by title from the Commons API
        (``action=query&titles=File:…&prop=imageinfo``) and the
        direct file URL from ``imageinfo`` is streamed — the same
        re-fetch pattern as Pexels / Pixabay. The Rights gate was
        already re-checked in `download_candidate`, so reaching
        here means Rights approved this file on a CC BY / CC BY-SA
        / CC0 / public-domain licence.
        """
        settings = self.settings
        api_url = "https://commons.wikimedia.org/w/api.php"
        params = {"action": "query", "format": "json", "formatversion": "2",
                  "titles": file_title, "prop": "imageinfo",
                  "iiprop": "url|mime"}
        try:
            with self._client_factory(settings.footage_metadata_timeout_seconds,
                                      api_url) as client:
                response = client.get(api_url, params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"wikimedia file lookup failed: HTTP "
                       f"{exc.response.status_code} for {file_title!r}")
        except Exception as exc:  # noqa: BLE001 — network/JSON, reported not raised
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"wikimedia file lookup failed for "
                       f"{file_title!r}: {exc}")
        pages = (data.get("query", {}) or {}).get("pages") or []
        if isinstance(pages, dict):
            pages = list(pages.values())
        info = {}
        if isinstance(pages, list) and pages:
            imageinfo = pages[0].get("imageinfo") or []
            if isinstance(imageinfo, list) and imageinfo:
                info = imageinfo[0] if isinstance(imageinfo[0], dict) else {}
        file_url = str(info.get("url") or "").strip()
        mime = str(info.get("mime") or "").strip().lower()
        if not file_url or mime not in ("video/mp4", "video/webm", "video/ogg"):
            return FootageDownload(
                candidate_video_id=candidate_id, status="failed",
                reason=f"wikimedia returned no downloadable video file "
                       f"for {file_title!r}")
        base_name = Path(urlparse(file_url).path).name
        if Path(base_name).suffix.lower() not in VIDEO_EXTENSIONS:
            base_name = f"{file_title.replace(' ', '_')}.mp4"
        return self._download_direct_file(job_id, candidate_id, file_url,
                                          base_name, budget_bytes)

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
