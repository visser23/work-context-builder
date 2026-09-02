"""Local folder source — scans filesystem directories (with subfolders).

Treats any set of local directories as a source. Useful for pointing at
OneDrive sync folders, local document archives, project directories, etc.

Self-aware: automatically skips its own database/state directories and
the output corpus if found within the scanned paths.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path

from workctx.config import LocalFolderSource
from workctx.models import (
    ChangeAction,
    DiscoveredChange,
    SourceObject,
    SourceType,
    SyncCheckpoint,
)
from workctx.normalise.convertibility import can_convert
from workctx.sources.base import Source
from workctx.state import StateDB

logger = logging.getLogger(__name__)

_SELF_MARKERS = {
    "state.sqlite",
    "run.lock",
    "notification_state.json",
}


class LocalFolderAdapter(Source):
    """Scans local filesystem folders and their subfolders."""

    def __init__(
        self,
        config: LocalFolderSource,
        state_dir: Path | None = None,
        output_root: Path | None = None,
    ) -> None:
        self.config = config
        self._paths = [Path(p).expanduser().resolve() for p in config.paths]
        self._state_dir = state_dir.resolve() if state_dir else None
        self._output_root = output_root.resolve() if output_root else None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def source_type(self) -> SourceType:
        return SourceType.LOCAL_FOLDER

    def validate(self) -> list[str]:
        issues: list[str] = []
        for p in self._paths:
            if not p.exists():
                issues.append(f"{self.name}: path does not exist: {p}")
            elif not p.is_dir():
                issues.append(f"{self.name}: path is not a directory: {p}")
        return issues

    def discover_changes(
        self,
        db: StateDB,
        checkpoint: SyncCheckpoint | None,
        *,
        full: bool = False,
    ) -> list[DiscoveredChange]:
        known_objects = {obj.source_id: obj for obj in db.get_objects_for_source(self.name)}
        changes: list[DiscoveredChange] = []
        seen_ids: set[str] = set()

        for file_path, rel_path in self._walk_all():
            source_id = rel_path
            seen_ids.add(source_id)

            try:
                stat = file_path.stat()
            except OSError:
                continue

            existing = known_objects.get(source_id)

            if full or not existing:
                action = ChangeAction.ADD if not existing else ChangeAction.UPDATE
                changes.append(self._make_change(file_path, rel_path, stat, action))
                continue

            if self._metadata_changed(existing, stat):
                content_hash = self._hash_file(file_path)
                if content_hash and content_hash != existing.content_sha256:
                    changes.append(
                        self._make_change(file_path, rel_path, stat, ChangeAction.UPDATE)
                    )
                elif content_hash:
                    db.upsert_object(
                        SourceObject(
                            source_name=self.name,
                            source_type=self.source_type,
                            source_id=source_id,
                            file_size=stat.st_size,
                            file_mtime=stat.st_mtime,
                            content_sha256=content_hash,
                            output_path=existing.output_path,
                            title=existing.title,
                            source_url=existing.source_url,
                            last_processed_at=existing.last_processed_at,
                        )
                    )

        for source_id, obj in known_objects.items():
            if source_id not in seen_ids:
                changes.append(
                    DiscoveredChange(
                        source_id=source_id,
                        title=obj.title,
                        action=ChangeAction.DELETE,
                    )
                )

        logger.info(
            "LocalFolder/%s: %d files scanned, %d changes detected",
            self.name,
            len(seen_ids),
            len(changes),
        )
        return changes

    def get_current_ids(self) -> set[str]:
        return {rel for _, rel in self._walk_all()}

    def _walk_all(self) -> list[tuple[Path, str]]:
        """Walk all configured paths, returning (absolute_path, relative_id) pairs."""
        files: list[tuple[Path, str]] = []

        for base_path in self._paths:
            if not base_path.exists() or not base_path.is_dir():
                continue

            for root, dirs, filenames in os.walk(base_path):
                root_path = Path(root).resolve()

                if self._should_skip_dir(root_path):
                    dirs.clear()
                    continue

                dirs[:] = [d for d in dirs if not self._should_skip_dir(root_path / d)]

                for fname in filenames:
                    if fname.startswith(".") or fname.startswith("~$"):
                        continue

                    file_path = root_path / fname
                    rel = str(file_path.relative_to(base_path))

                    if self._is_excluded(rel):
                        continue
                    if not self._is_included(rel):
                        continue
                    if not can_convert(fname):
                        continue

                    files.append((file_path, f"{base_path.name}/{rel}"))

        return files

    def _should_skip_dir(self, dir_path: Path) -> bool:
        """Skip our own state/output directories and common junk."""
        resolved = dir_path.resolve()
        if self._state_dir and resolved == self._state_dir:
            return True
        if self._state_dir and self._state_dir in resolved.parents:
            return True
        if self._output_root and resolved == self._output_root:
            return True
        if self._output_root and self._output_root in resolved.parents:
            return True

        name = dir_path.name
        if name in {
            ".git",
            "__pycache__",
            "node_modules",
            ".venv",
            "venv",
            ".tox",
            ".mypy_cache",
            ".ruff_cache",
            ".pytest_cache",
            ".DS_Store",
            "$RECYCLE.BIN",
            "System Volume Information",
        }:
            return True

        return any(marker in os.listdir(dir_path) for marker in _SELF_MARKERS if dir_path.exists())

    def _is_excluded(self, rel_path: str) -> bool:
        fname = os.path.basename(rel_path)
        for pattern in self.config.exclude:
            base = _strip_glob(pattern)
            if fnmatch.fnmatch(rel_path, base) or fnmatch.fnmatch(fname, base):
                return True
        return False

    def _is_included(self, rel_path: str) -> bool:
        if not self.config.include:
            return True
        for pattern in self.config.include:
            if pattern in ("**/*", "**"):
                return True
            base = _strip_glob(pattern)
            if fnmatch.fnmatch(rel_path, base) or fnmatch.fnmatch(os.path.basename(rel_path), base):
                return True
        return False

    def _metadata_changed(self, existing: SourceObject, stat: os.stat_result) -> bool:
        if existing.file_size is not None and existing.file_size != stat.st_size:
            return True
        return bool(
            existing.file_mtime is not None and abs(existing.file_mtime - stat.st_mtime) > 1.0
        )

    def _make_change(
        self,
        file_path: Path,
        rel_path: str,
        stat: os.stat_result,
        action: ChangeAction,
    ) -> DiscoveredChange:
        return DiscoveredChange(
            source_id=rel_path,
            title=file_path.stem,
            action=action,
            local_path=str(file_path),
            file_size=stat.st_size,
            file_mtime=stat.st_mtime,
            source_updated_at=datetime.fromtimestamp(stat.st_mtime),
        )

    def _hash_file(self, file_path: Path) -> str | None:
        try:
            h = hashlib.sha256()
            with open(file_path, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None


def _strip_glob(pattern: str) -> str:
    while pattern.startswith("**/"):
        pattern = pattern[3:]
    return pattern
