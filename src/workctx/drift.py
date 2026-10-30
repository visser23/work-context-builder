"""Detect and repair *drift*: indexed documents whose files are gone from disk.

The state DB lives outside the (usually cloud-synced) output folder, so the two can
disagree: the cloud client resets or restores the folder, a user deletes files, a disk is
swapped. Incremental discovery only looks at what changed at the source, so on its own it
never rebuilds a file that vanished locally and the corpus silently stays incomplete
(incident of Oct 2026: ~26k of ~30k files missing while the DB said everything was synced).

Every sync therefore checks the disk first and marks rows whose files are missing so that
the normal discovery path fetches and writes them again. The check is repeated every run,
so a document that fails to rebuild is retried until it succeeds.
"""

from __future__ import annotations

import logging
import os
import posixpath
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from workctx.corpus import document_matcher

if TYPE_CHECKING:
    from workctx.models import SourceObject
    from workctx.state import StateDB

logger = logging.getLogger(__name__)

MISSING_OUTPUT_ERROR = "missing:output_file"


def find_missing_outputs(output_root: Path, objects: Iterable[SourceObject]) -> list[SourceObject]:
    """Objects none of whose corpus files exist under ``output_root``.

    Each directory is listed once. Only "does not exist" counts as missing: any other
    filesystem error (a busy cloud folder answers EDEADLK/ETIMEDOUT) means *unknown*, and
    unknown is treated as present so a flaky mount can never trigger a mass re-download.
    """
    by_dir: dict[str, list[tuple[SourceObject, Callable[[str], bool]]]] = defaultdict(list)
    for o in objects:
        if not o.output_path:
            continue
        parent, is_member = document_matcher(o.output_path)
        by_dir[parent].append((o, is_member))

    missing: list[SourceObject] = []
    unreadable = 0
    for parent, entries in by_dir.items():
        try:
            names = os.listdir(output_root / parent if parent else output_root)
        except (FileNotFoundError, NotADirectoryError):
            missing.extend(o for o, _ in entries)
            continue
        except OSError as exc:
            unreadable += len(entries)
            logger.debug("Cannot list %s (%s); not treating its files as missing", parent, exc)
            continue
        missing.extend(o for o, is_member in entries if not any(is_member(n) for n in names))
    if unreadable:
        logger.warning(
            "Drift check: %d document(s) in unreadable folders were not checked", unreadable
        )
    return missing


def repair_drift(source_name: str, db: StateDB, output_root: Path, *, dry_run: bool = False) -> int:
    """Mark every document of ``source_name`` whose file is missing for re-fetching.

    Returns how many are missing. With ``dry_run`` the DB is left untouched.
    """
    missing = find_missing_outputs(output_root, db.get_objects_for_source(source_name))
    if not missing:
        return 0
    sample = posixpath.basename(missing[0].output_path or "")
    logger.warning(
        "%s: %d indexed file(s) are missing on disk (e.g. %s) — %s",
        source_name,
        len(missing),
        sample[:80],
        "would be re-fetched" if dry_run else "re-fetching them from the source",
    )
    if not dry_run:
        db.mark_for_refetch(source_name, [o.source_id for o in missing])
    return len(missing)


def drift_report(db: StateDB, output_root: Path) -> dict[str, tuple[int, int]]:
    """``{source_name: (indexed_documents, missing_on_disk)}`` — read-only."""
    names = [
        r["source_name"]
        for r in db.conn.execute("SELECT DISTINCT source_name FROM source_objects ORDER BY 1")
    ]
    report: dict[str, tuple[int, int]] = {}
    for name in names:
        objects = db.get_objects_for_source(name)
        report[name] = (len(objects), len(find_missing_outputs(output_root, objects)))
    return report
