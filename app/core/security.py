"""Security helpers for RANKFLOW AI.

- API keys are read only from environment variables (see core.config).
- Uploaded / generated files must stay inside the configured storage
  root; `safe_join` rejects path traversal.
- Topic / free-text input is length-limited and stripped of control
  characters before it reaches any agent or external service.
"""
from __future__ import annotations

import re
from pathlib import Path

from .errors import ValidationError

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_join(root: Path, *parts: str) -> Path:
    """Join path parts under `root`, rejecting traversal outside it."""
    root = root.resolve()
    target = (root.joinpath(*parts)).resolve()
    if root not in target.parents and target != root:
        raise ValidationError(f"path escapes storage root: {'/'.join(parts)}")
    return target


def clean_text(value: str, *, max_length: int = 200, field: str = "text") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    if not cleaned:
        raise ValidationError(f"{field} must not be empty")
    if len(cleaned) > max_length:
        raise ValidationError(f"{field} must be at most {max_length} characters")
    return cleaned


def require_env(name: str) -> str:
    """Read a secret from the environment; fail loudly if it is missing."""
    import os

    value = os.environ.get(name, "")
    if not value:
        from .errors import ConfigurationError

        raise ConfigurationError(f"environment variable {name} is not set")
    return value
