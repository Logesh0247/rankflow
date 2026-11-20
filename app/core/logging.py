"""Logging configuration for RANKFLOW AI.

Single JSON-ish plain-text format, one logger per module, level from
settings. Never log API keys, tokens or full file contents: a
redacting filter masks values that look like secrets in any log
record (Prompt 12, Phase 5).
"""
from __future__ import annotations

import logging
import re
import sys

_CONFIGURED = False

# Key-shaped values in log text.
_SECRET_PATTERNS = [
    # api_key=..., token: "...", password='...' -> key=***
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\",\s]+"),
     r"\1=***"),
    # Authorization: Bearer <token> -> Bearer ***
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]+"), "Bearer ***"),
    # Webhook URLs carry a token in the path -> keep the host and
    # the webhook type segment, mask the rest. Covers both
    # https://hooks.slack.com/services/T.../A.../xyz and
    # https://discord.com/api/webhooks/<id>/<token>.
    (re.compile(r"(?i)(https?://[^\s/]+/(?:hooks?|services|api/webhooks)/)[^\s'\"]+"), r"\1***"),
]


def _redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Mask secret-shaped values in log records before they are emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            try:
                record.args = tuple(
                    _redact(a) if isinstance(a, str) else a for a in record.args)
            except TypeError:
                pass
        return True


def configure_logging(level: str = "INFO") -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.addFilter(RedactSecretsFilter())
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            handlers=[handler],
            force=True,
        )
        _CONFIGURED = True
    logger = logging.getLogger("rankflow")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"rankflow.{name}")
