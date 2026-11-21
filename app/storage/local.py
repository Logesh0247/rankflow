"""Momentrix AI storage — local filesystem backend (Prompt 01).

Interface: `LocalStorage` implements `put / get_path / exists / delete`
under a single root directory. An S3-compatible backend is an
integration point for later prompts; it must implement the same
interface and is NOT implemented here.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from app.core.errors import StorageError
from app.core.security import safe_join


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, relative_path: str, data: bytes) -> Path:
        target = safe_join(self.root, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(data)
        except OSError as exc:
            raise StorageError(f"failed to write {relative_path}: {exc}") from exc
        return target

    def put_file(self, relative_path: str, source: Path) -> Path:
        target = safe_join(self.root, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return target

    def get_path(self, relative_path: str) -> Path:
        target = safe_join(self.root, relative_path)
        if not target.exists():
            raise StorageError(f"file not found: {relative_path}")
        return target

    def exists(self, relative_path: str) -> bool:
        return safe_join(self.root, relative_path).exists()

    def delete(self, relative_path: str) -> None:
        target = safe_join(self.root, relative_path)
        if target.exists():
            target.unlink()


def get_storage(settings=None):
    """Return the configured storage backend. Only 'local' exists in Prompt 01."""
    from app.core.config import get_settings

    settings = settings or get_settings()
    if settings.storage_backend == "local":
        return LocalStorage(settings.storage_root)
    raise StorageError(f"storage backend not implemented in Prompt 01: {settings.storage_backend}")
