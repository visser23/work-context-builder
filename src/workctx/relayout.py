"""Move an existing SharePoint corpus into the shorter on-disk layout.

Older versions wrote SharePoint documents under
``sharepoint/<source>/sites/<site>/<library>/<root folder>/...`` — five levels of
folders that every file shares — and, because that pushed deep files past OneDrive's
path limit, mangled some directory names (``Digital _9c2e``) so one folder could exist
under several names. Documents now live directly under ``sharepoint/<source>/``.

``relayout_sharepoint`` renames the already-converted Markdown into that layout
without re-downloading anything. It is idempotent and resumable: each file is moved,
then the state database and search index are re-pointed, so an interrupted run simply
continues next time.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from workctx.config import ProjectConfig
from workctx.corpus import _TRANSIENT_WRITE_ERRNOS, build_output_path, clamp_output_path
from workctx.indexing import SearchIndex
from workctx.models import SourceType
from workctx.sources.sharepoint_web import SharePointWebSource
from workctx.state import StateDB

logger = logging.getLogger(__name__)

_PART_RE = re.compile(r"^(?P<stem>.*)\.part-\d{3}(?P<ext>\.[^./]+)$")


@dataclass
class RelayoutReport:
    moved: int = 0
    already_ok: int = 0
    reset: int = 0
    """Stored file missing on disk: the row was dropped so the next sync re-fetches it."""
    conflicts: list[str] = field(default_factory=list)
    planned: list[tuple[str, str]] = field(default_factory=list)


def relayout_sharepoint(
    config: ProjectConfig,
    db: StateDB,
    idx: SearchIndex,
    *,
    dry_run: bool = False,
    progress_every: int = 250,
) -> RelayoutReport:
    """Move every browser-mode SharePoint document to its new location."""
    report = RelayoutReport()
    output_root = config.output_root_path

    for sp_config in config.sources.sharepoint:
        if sp_config.mode != "browser":
            continue
        source = SharePointWebSource(sp_config)
        objects = [o for o in db.get_objects_for_source(source.name) if o.output_path]

        plans: list[tuple[str, str, str]] = []  # (source_id, old_stored, new_base)
        claimed: dict[str, str] = {}
        for obj in objects:
            assert obj.output_path is not None
            old_stored = obj.output_path
            part = _PART_RE.match(old_stored)
            old_base = f"{part['stem']}{part['ext']}" if part else old_stored
            new_base = clamp_output_path(
                output_root,
                build_output_path(
                    SourceType.SHAREPOINT,
                    source.name,
                    obj.source_id,
                    relative_source_path=source.relative_path(obj.source_id),
                ),
            )
            other = claimed.setdefault(new_base, obj.source_id)
            if other != obj.source_id:
                report.conflicts.append(f"{obj.source_id} and {other} both map to {new_base}")
                continue
            if new_base == old_base:
                report.already_ok += 1
                continue
            plans.append((obj.source_id, old_stored, new_base))

        for n, (source_id, old_stored, new_base) in enumerate(plans, 1):
            _relocate_one(
                db,
                idx,
                output_root,
                source.name,
                source_id,
                old_stored,
                new_base,
                report,
                dry_run=dry_run,
            )
            if progress_every and n % progress_every == 0:
                logger.info("Relayout %s: %d/%d", source.name, n, len(plans))

    logger.info(
        "Relayout complete: moved=%d already_ok=%d reset=%d conflicts=%d%s",
        report.moved,
        report.already_ok,
        report.reset,
        len(report.conflicts),
        " (dry run)" if dry_run else "",
    )
    return report


def _relocate_one(
    db: StateDB,
    idx: SearchIndex,
    output_root: Path,
    source_name: str,
    source_id: str,
    old_stored: str,
    new_base: str,
    report: RelayoutReport,
    *,
    dry_run: bool,
) -> None:
    part = _PART_RE.match(old_stored)
    raw_base = f"{part['stem']}{part['ext']}" if part else old_stored
    # Legacy paths were stored with a doubled slash ("sharepoint/src//sites/...").
    # Disk listings come back normalised, so work on the normalised form and keep the raw
    # one only to find the matching search-index rows.
    raw_prefix, ext = os.path.splitext(raw_base)
    old_prefix = posixpath.normpath(raw_prefix)
    new_prefix = os.path.splitext(new_base)[0]

    old_files = _files_with_prefix(output_root, old_prefix, ext)
    if not old_files:
        logger.warning("Relayout: %s has no file on disk — will be re-fetched", source_id)
        report.reset += 1
        if not dry_run:
            idx.remove(old_stored)
            db.delete_object(source_name, source_id)
        return

    moves = [(rel, f"{new_prefix}{rel[len(old_prefix) :]}") for rel in old_files]
    for _, dst in moves:
        if (output_root / dst).exists():
            report.conflicts.append(f"{source_id}: target already exists: {dst}")
            return

    report.planned.extend(moves)
    if dry_run:
        report.moved += 1
        return

    for src_rel, dst_rel in moves:
        _move(output_root / src_rel, output_root / dst_rel)
        raw_rel = f"{raw_prefix}{src_rel[len(old_prefix) :]}"
        idx.rename(raw_rel, dst_rel)
        if raw_rel != src_rel:
            idx.rename(src_rel, dst_rel)
        _prune_empty_dirs(output_root, (output_root / src_rel).parent)

    suffix = old_stored[len(raw_prefix) :]
    db.update_output_path(source_name, source_id, f"{new_prefix}{suffix}")
    report.moved += 1


def _files_with_prefix(output_root: Path, prefix: str, ext: str) -> list[str]:
    """``prefix + ext`` and ``prefix.part-NNN + ext`` that exist (no glob: names hold [ ] etc.)."""
    directory = output_root / Path(prefix).parent
    stem = Path(prefix).name
    found: list[str] = []
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return found
    # Any other OSError (File Provider timeout, permissions) propagates: an unreadable
    # corpus must abort the move, never be mistaken for "the file is gone".
    parent = str(Path(prefix).parent)
    for name in names:
        if name == f"{stem}{ext}" or re.fullmatch(
            re.escape(stem) + r"\.part-\d{3}" + re.escape(ext), name
        ):
            found.append(name if parent == "." else f"{parent}/{name}")
    return sorted(found)


def _move(src: Path, dst: Path, *, attempts: int = 5) -> None:
    """Rename a corpus file, riding out the File Provider's transient errors."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if exc.errno not in _TRANSIENT_WRITE_ERRNOS or attempt == attempts:
                raise
            logger.warning("Transient error moving %s (attempt %d): %s", src.name, attempt, exc)
            time.sleep(2.0 * attempt)


def _prune_empty_dirs(output_root: Path, directory: Path) -> None:
    """Remove ``directory`` and empty ancestors (a lone ``.DS_Store`` does not count)."""
    root = output_root.resolve()
    current = directory.resolve()
    while current != root and root in current.parents:
        try:
            entries = set(os.listdir(current))
        except OSError:
            return
        if not entries <= {".DS_Store"}:
            return
        try:
            (current / ".DS_Store").unlink(missing_ok=True)
            current.rmdir()
        except OSError:
            return
        current = current.parent
