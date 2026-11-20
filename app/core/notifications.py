"""Failure notifications (Prompt 12, Phase 4).

When a job fails, the failure is never silent: it is logged, it is
appended as one JSON line to `data/notifications.log`, and — only
when `NOTIFY_WEBHOOK_URL` is configured — it is POSTed to that
webhook (e.g. a Slack/Discord incoming webhook) with a short
timeout. Nothing here retries, blocks a job, or raises: a
notification that cannot be delivered is logged and dropped.
No secret is ever included in a notification payload.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger("notifications")


def _log_path(settings: Settings) -> Path:
    return Path(settings.storage_root).parent / "notifications.log"


def notify_job_failed(job_id: str, stage: str, error: str,
                      settings: Settings | None = None) -> dict:
    """Record a job failure. Returns what was recorded (never raises)."""
    settings = settings or get_settings()
    event = {
        "event": "job_failed",
        "job_id": job_id,
        "stage": stage,
        "error": (error or "")[:500],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path = _log_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError as exc:
        logger.warning("could not append failure notification: %s", exc)
    webhook = (settings.notify_webhook_url or "").strip()
    if webhook:
        try:
            import httpx
            httpx.post(webhook, json=event, timeout=5.0)
            event["webhook"] = "posted"
        except Exception as exc:  # noqa: BLE001 — best-effort only
            logger.warning("failure webhook post failed: %s", exc)
            event["webhook"] = "failed"
    logger.error("job %s failed at stage %s: %s", job_id, stage, error)
    return event


def recent_notifications(settings: Settings | None = None, limit: int = 20) -> list[dict]:
    """Last `limit` failure notifications from the log (newest first)."""
    settings = settings or get_settings()
    path = _log_path(settings)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    events = []
    for line in reversed(lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
