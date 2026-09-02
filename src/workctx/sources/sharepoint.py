"""SharePoint source adapter — onedrive_local mode.

Reads a local OneDrive-synced SharePoint directory and detects changes
using filesystem metadata (mtime + size) before calculating content hashes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import time
from pathlib import Path

from workctx.config import SharePointSource
from workctx.models import (
    ChangeAction,
    DiscoveredChange,
    SourceObject,
    SourceType,
    SyncCheckpoint,
)
from workctx.sources.base import Source
from workctx.state import StateDB

logger = logging.getLogger(__name__)

MATERIALISE_TIMEOUT = 30
MATERIALISE_POLL_INTERVAL = 2


def _strip_glob_prefix(pattern: str) -> str:
    """Strip leading **/ from a glob pattern for basename matching."""
    while pattern.startswith("**/"):
        pattern = pattern[3:]
    return pattern


class SharePointLocalSource(Source):
    """SharePoint via OneDrive local sync (onedrive_local mode)."""

    def __init__(self, config: SharePointSource) -> None:
        self.config = config
        self._local_path = Path(config.local_path) if config.local_path else None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def source_type(self) -> SourceType:
        return SourceType.SHAREPOINT

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self._local_path:
            issues.append(f"{self.name}: local_path is required for onedrive_local mode")
        elif not self._local_path.exists():
            issues.append(f"{self.name}: local_path does not exist: {self._local_path}")
        elif not self._local_path.is_dir():
            issues.append(f"{self.name}: local_path is not a directory: {self._local_path}")
        return issues

    def discover_changes(
        self,
        db: StateDB,
        checkpoint: SyncCheckpoint | None,
        *,
        full: bool = False,
    ) -> list[DiscoveredChange]:
        if not self._local_path or not self._local_path.exists():
            logger.error("SharePoint local path unavailable: %s", self._local_path)
            return []

        known_objects = {obj.source_id: obj for obj in db.get_objects_for_source(self.name)}
        changes: list[DiscoveredChange] = []
        seen_ids: set[str] = set()

        for file_path in self._walk_files():
            rel_path = str(file_path.relative_to(self._local_path))
            source_id = rel_path
            seen_ids.add(source_id)

            try:
                stat = file_path.stat()
            except OSError:
                logger.warning("Cannot stat file: %s", file_path)
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
            "SharePoint/%s: %d files scanned, %d changes detected",
            self.name,
            len(seen_ids),
            len(changes),
        )
        return changes

    def get_current_ids(self) -> set[str]:
        if not self._local_path or not self._local_path.exists():
            return set()
        return {str(f.relative_to(self._local_path)) for f in self._walk_files()}

    def _walk_files(self) -> list[Path]:
        """Walk the local directory, applying include/exclude filters."""
        if not self._local_path:
            return []
        files: list[Path] = []
        for root, _dirs, filenames in os.walk(self._local_path):
            for fname in filenames:
                file_path = Path(root) / fname
                rel = str(file_path.relative_to(self._local_path))

                if self._is_excluded(rel):
                    continue
                if not self._is_included(rel):
                    continue

                files.append(file_path)
        return files

    def _is_excluded(self, rel_path: str) -> bool:
        fname = os.path.basename(rel_path)
        for pattern in self.config.exclude:
            base = _strip_glob_prefix(pattern)
            if fnmatch.fnmatch(rel_path, base):
                return True
            if fnmatch.fnmatch(fname, base):
                return True
        return False

    def _is_included(self, rel_path: str) -> bool:
        if not self.config.include:
            return True
        for pattern in self.config.include:
            if pattern in ("**/*", "**"):
                return True
            base = _strip_glob_prefix(pattern)
            if fnmatch.fnmatch(rel_path, base):
                return True
            if fnmatch.fnmatch(os.path.basename(rel_path), base):
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
        title = file_path.stem
        site_url = self.config.site_url or ""
        source_url = f"{site_url}/{rel_path}" if site_url else None
        return DiscoveredChange(
            source_id=rel_path,
            title=title,
            source_url=source_url,
            action=action,
            local_path=str(file_path),
            file_size=stat.st_size,
            file_mtime=stat.st_mtime,
        )

    def _hash_file(self, file_path: Path) -> str | None:
        """Calculate SHA-256, materialising cloud-only files if needed."""
        file_path = self._ensure_materialised(file_path)
        if not file_path:
            return None
        try:
            h = hashlib.sha256()
            with open(file_path, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            logger.warning("Cannot hash file: %s", file_path)
            return None

    def _ensure_materialised(self, file_path: Path) -> Path | None:
        """Ensure a Files On-Demand placeholder is downloaded.

        Accessing the file normally will trigger OneDrive to materialise it.
        We poll with a timeout.
        """
        deadline = time.monotonic() + MATERIALISE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                with open(file_path, "rb") as f:
                    f.read(1)
                return file_path
            except OSError:
                logger.debug("Waiting for materialisation: %s", file_path)
                time.sleep(MATERIALISE_POLL_INTERVAL)

        logger.warning(
            "File did not materialise within %ds: %s",
            MATERIALISE_TIMEOUT,
            file_path,
        )
        return None
