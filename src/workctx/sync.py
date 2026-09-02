"""Sync orchestration: discover → fetch → normalise → write → index."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from workctx.config import ProjectConfig
from workctx.corpus import (
    build_output_path,
    generate_agents_md,
    generate_chatgpt_instructions,
    generate_claude_md,
    generate_context_md,
    generate_health,
    generate_index_md,
    generate_jira_summary,
    generate_manifest,
    generate_project_brief,
    generate_readme,
    remove_corpus_file,
    write_corpus_file,
)
from workctx.indexing import SearchIndex
from workctx.locking import ExecutionLock, LockError
from workctx.models import (
    ChangeAction,
    DiscoveredChange,
    FrontMatter,
    RunStatus,
    SourceObject,
    SourceResult,
    SourceType,
    SyncCheckpoint,
    SyncResult,
)
from workctx.normalise.common import content_hash, split_large_document, wrap_with_front_matter
from workctx.progress import SyncProgress
from workctx.sources.base import Source
from workctx.state import StateDB

logger = logging.getLogger(__name__)

_HEAVY_CONVERSION_LIMIT = 3
_HEAVY_CONVERSION_SEMAPHORE = threading.Semaphore(_HEAVY_CONVERSION_LIMIT)
_DB_WRITE_LOCK = threading.Lock()


def run_sync(
    config: ProjectConfig,
    *,
    run_id: str,
    dry_run: bool = False,
    full: bool = False,
    quiet: bool = False,
) -> SyncResult:
    """Execute a full sync across all configured sources."""

    result = SyncResult(
        run_id=run_id,
        started_at=datetime.now(UTC),
    )

    lock = ExecutionLock(config.state_dir / "run.lock")
    try:
        lock.acquire()
    except LockError as e:
        logger.error("Lock acquisition failed: %s", e)
        result.status = RunStatus.FAILED
        result.completed_at = datetime.now(UTC)
        return result

    progress = SyncProgress(quiet=quiet or dry_run)
    db: StateDB | None = None
    idx: SearchIndex | None = None
    sources: list[Source] = []

    try:
        db = StateDB(config.state_dir / "state.sqlite")
        idx = SearchIndex(config.state_dir / "state.sqlite")
        output_root = config.output_root_path
        output_root.mkdir(parents=True, exist_ok=True)

        sources = _build_sources(config)

        max_workers = max(1, config.sync.max_concurrency)
        with (
            progress.live(),
            ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="worker",
            ) as shared_pool,
            ThreadPoolExecutor(
                max_workers=len(sources), thread_name_prefix="source",
            ) as source_pool,
        ):
            futures = {
                source_pool.submit(
                    _sync_source,
                    source, config, db, idx, output_root,
                    dry_run=dry_run, full=full,
                    progress=progress, worker_pool=shared_pool,
                    max_workers=max_workers,
                ): source
                for source in sources
            }
            for future in as_completed(futures):
                result.source_results.append(future.result())

        if not dry_run:
            generate_manifest(db, output_root)
            generate_health(db, output_root, result.aggregate_status().value)
            generate_index_md(config, db, output_root)
            generate_jira_summary(config, db, output_root)
            generate_context_md(config, output_root)
            generate_agents_md(config, output_root)
            generate_claude_md(config, output_root)
            generate_chatgpt_instructions(config, output_root)
            generate_project_brief(config, db, output_root)
            generate_readme(config, output_root)

        result.completed_at = datetime.now(UTC)
        result.status = result.aggregate_status()

        if not quiet:
            progress.print_summary()

    except Exception:
        logger.exception("Sync failed with unhandled exception")
        result.status = RunStatus.FAILED
        result.completed_at = datetime.now(UTC)
    finally:
        for source in sources:
            with contextlib.suppress(Exception):
                source.close()
        if db:
            db.close()
        if idx:
            idx.close()
        lock.release()

    return result


def run_reconciliation(config: ProjectConfig, *, run_id: str) -> None:
    """Force reconciliation across all sources to detect deletions."""
    db = StateDB(config.state_dir / "state.sqlite")
    idx = SearchIndex(config.state_dir / "state.sqlite")
    sources: list[Source] = []
    try:
        output_root = config.output_root_path
        sources = _build_sources(config)
        for source in sources:
            _reconcile_source(source, db, idx, output_root)
    finally:
        for source in sources:
            with contextlib.suppress(Exception):
                source.close()
        db.close()
        idx.close()


def _build_sources(config: ProjectConfig) -> list[Source]:
    """Instantiate source adapters from configuration."""
    from workctx.sources.confluence import ConfluenceAdapter
    from workctx.sources.jira import JiraAdapter
    from workctx.sources.local_folder import LocalFolderAdapter
    from workctx.sources.sharepoint import SharePointLocalSource
    from workctx.sources.sharepoint_web import SharePointWebSource

    sources: list[Source] = []
    overlap = config.sync.overlap_minutes
    max_workers = max(1, config.sync.max_concurrency)

    for sp_config in config.sources.sharepoint:
        if sp_config.mode == "onedrive_local":
            sources.append(SharePointLocalSource(sp_config))
        elif sp_config.mode == "browser":
            sources.append(SharePointWebSource(sp_config, max_workers=max_workers))

    for jira_config in config.sources.jira:
        sources.append(JiraAdapter(jira_config, overlap_minutes=overlap))

    for conf_config in config.sources.confluence:
        sources.append(ConfluenceAdapter(conf_config, overlap_minutes=overlap))

    for lf_config in config.sources.local_folders:
        sources.append(
            LocalFolderAdapter(
                lf_config,
                state_dir=config.state_dir,
                output_root=config.output_root_path,
            )
        )

    return sources


def _sync_source(
    source: Source,
    config: ProjectConfig,
    db: StateDB,
    idx: SearchIndex,
    output_root: Path,
    *,
    dry_run: bool = False,
    full: bool = False,
    progress: SyncProgress | None = None,
    worker_pool: ThreadPoolExecutor | None = None,
    max_workers: int = 4,
) -> SourceResult:
    """Sync a single source."""
    sr = SourceResult(source_name=source.name, source_type=source.source_type)

    try:
        checkpoint = db.get_checkpoint(source.name)
        should_reconcile = _should_reconcile(checkpoint, config.sync.reconciliation_days)

        if progress:
            progress.begin_discovery(source.name)

        changes = source.discover_changes(db, checkpoint, full=full)
        sr.objects_checked = len(changes)

        if progress:
            progress.end_discovery(source.name, len(changes))

        if dry_run:
            for c in changes:
                if c.action == ChangeAction.ADD:
                    sr.objects_added += 1
                elif c.action == ChangeAction.UPDATE:
                    sr.objects_updated += 1
                elif c.action == ChangeAction.DELETE:
                    sr.objects_deleted += 1
            return sr

        latest_success_ts: str | None = None
        earliest_failure_ts: str | None = None

        deletes = [c for c in changes if c.action == ChangeAction.DELETE]
        upserts = [c for c in changes if c.action != ChangeAction.DELETE]

        for change in deletes:
            try:
                _handle_delete(source, change, db, idx, output_root)
                sr.objects_deleted += 1
                if progress:
                    progress.advance(source.name, deleted=1)
                if change.source_updated_at:
                    ts = change.source_updated_at.isoformat()
                    if latest_success_ts is None or ts > latest_success_ts:
                        latest_success_ts = ts
            except Exception as e:
                sr.objects_failed += 1
                sr.errors.append(f"{change.source_id}: {e}")
                if progress:
                    progress.advance(source.name, failed=1)
                logger.error("Failed to delete %s/%s: %s", source.name, change.source_id, e)

        def _process_one(
            change: DiscoveredChange,
        ) -> tuple[DiscoveredChange, str | None, bool, Exception | None]:
            try:
                body_md, is_stub = _fetch_and_convert(source, change)
                return (change, body_md, is_stub, None)
            except Exception as exc:
                return (change, None, True, exc)

        pool = worker_pool or ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {pool.submit(_process_one, c): c for c in upserts}
            for future in as_completed(futures):
                change, body_md, is_stub, exc = future.result()
                if exc:
                    if change.source_updated_at:
                        ts = change.source_updated_at.isoformat()
                        if earliest_failure_ts is None or ts < earliest_failure_ts:
                            earliest_failure_ts = ts
                    sr.objects_failed += 1
                    sr.errors.append(f"{change.source_id}: {exc}")
                    if progress:
                        progress.advance(source.name, failed=1)
                    logger.error(
                        "Failed to process %s/%s: %s",
                        source.name,
                        change.source_id,
                        exc,
                        exc_info=True,
                    )
                    continue

                try:
                    _write_and_index(
                        source, change, config, db, idx, output_root,
                        body_md, is_stub,
                    )
                    if change.action == ChangeAction.ADD:
                        sr.objects_added += 1
                        if progress:
                            progress.advance(source.name, added=1)
                    else:
                        sr.objects_updated += 1
                        if progress:
                            progress.advance(source.name, updated=1)

                    if change.source_updated_at:
                        ts = change.source_updated_at.isoformat()
                        if latest_success_ts is None or ts > latest_success_ts:
                            latest_success_ts = ts

                except Exception as e:
                    if change.source_updated_at:
                        ts = change.source_updated_at.isoformat()
                        if earliest_failure_ts is None or ts < earliest_failure_ts:
                            earliest_failure_ts = ts
                    sr.objects_failed += 1
                    sr.errors.append(f"{change.source_id}: {e}")
                    if progress:
                        progress.advance(source.name, failed=1)
                    logger.error(
                        "Failed to write %s/%s: %s",
                        source.name,
                        change.source_id,
                        e,
                        exc_info=True,
                    )
        finally:
            if not worker_pool:
                pool.shutdown(wait=True)

        if should_reconcile and not full:
            _reconcile_source(source, db, idx, output_root)

        now = datetime.now(UTC)

        # Don't advance the checkpoint past any failed objects — rewind to
        # just before the earliest failure so those items are retried next run.
        if earliest_failure_ts and (
            latest_success_ts is None or earliest_failure_ts <= latest_success_ts
        ):
            cp_value = checkpoint.last_checkpoint if checkpoint else None
        else:
            cp_value = latest_success_ts or (checkpoint.last_checkpoint if checkpoint else None)
        cp_metadata = dict(checkpoint.metadata) if checkpoint and checkpoint.metadata else {}
        change_token = getattr(source, "_latest_change_token", None)
        if change_token:
            cp_metadata["change_token"] = change_token

        new_cp = SyncCheckpoint(
            source_name=source.name,
            source_type=source.source_type,
            last_checkpoint=cp_value,
            last_success=now
            if sr.objects_failed == 0
            else (checkpoint.last_success if checkpoint else None),
            last_reconciliation=now
            if should_reconcile
            else (checkpoint.last_reconciliation if checkpoint else None),
            metadata=cp_metadata,
        )
        db.save_checkpoint(new_cp)

        if sr.objects_failed > 0:
            sr.status = RunStatus.DEGRADED
        else:
            sr.status = RunStatus.HEALTHY

        if progress:
            progress.finish_source(source.name, sr.status.value)

    except Exception as e:
        sr.status = RunStatus.FAILED
        sr.errors.append(str(e))
        logger.exception("Source %s failed: %s", source.name, e)

    return sr


def _fetch_and_convert(
    source: Source,
    change: DiscoveredChange,
) -> tuple[str | None, bool]:
    """Download/extract content and convert to Markdown. Thread-safe.

    Returns (body_md, is_stub).
    """
    label = change.source_key or change.title or change.source_id
    logger.info(
        "%s/%s: processing %s",
        source.source_type.value,
        source.name,
        label[:120],
    )

    body_md: str | None = None

    if change.content_text:
        body_md = change.content_text
    elif hasattr(source, "render_content") and source.render_content is not None:
        body_md = source.render_content(change)
    elif change.local_path:
        body_md = _convert_local_file(Path(change.local_path))
    elif change.content:
        body_md = change.content.decode("utf-8", errors="replace")
    elif source.source_type == SourceType.SHAREPOINT and hasattr(source, "download_file"):
        body_md = _download_and_convert(source, change)

    if not body_md:
        body_md = _make_unsupported_stub(change) if change.local_path else _make_empty_stub(change)
        logger.debug(
            "%s/%s: %s → stub", source.source_type.value, source.name, label[:80],
        )
        return body_md, True

    return body_md, False


def _write_and_index(
    source: Source,
    change: DiscoveredChange,
    config: ProjectConfig,
    db: StateDB,
    idx: SearchIndex,
    output_root: Path,
    body_md: str | None,
    is_stub: bool,
) -> None:
    """Write content to corpus and update DB/index. Thread-safe via _DB_WRITE_LOCK."""
    with _DB_WRITE_LOCK:
        change.metadata.pop("_raw_issue", None)

        if body_md is None:
            body_md = _make_empty_stub(change)
            is_stub = True

        label = change.source_key or change.title or change.source_id
        space = change.metadata.get("space")
        project = change.metadata.get("project")

        file_source_types = (SourceType.SHAREPOINT, SourceType.LOCAL_FOLDER)

        output_path = build_output_path(
            source.source_type,
            source.name,
            change.source_id,
            source_key=change.source_key,
            title=change.title,
            space=space,
            project=project,
            relative_source_path=(
                change.source_id if source.source_type in file_source_types else None
            ),
        )

        now = datetime.now(UTC)
        fm = FrontMatter(
            source_type=source.source_type.value,
            source_name=source.name,
            source_id=change.source_id,
            title=change.title or change.source_id,
            source_url=change.source_url,
            source_key=change.source_key,
            issue_key=change.source_key if source.source_type == SourceType.JIRA else None,
            project=project,
            space=space,
            source_path=(change.source_id if source.source_type in file_source_types else None),
            status=change.metadata.get("status"),
            source_version=change.source_version,
            updated_at=change.source_updated_at,
            synced_at=now,
        )

        full_content = wrap_with_front_matter(fm, body_md)
        c_hash = content_hash(full_content)

        existing = db.get_object(source.name, change.source_id)
        if existing and existing.content_sha256 == c_hash:
            if existing.source_version != change.source_version:
                db.update_version(source.name, change.source_id, change.source_version)
            logger.debug(
                "%s/%s: %s → unchanged",
                source.source_type.value, source.name, label[:80],
            )
            return

        parts = split_large_document(fm, body_md, config.sync.large_document_chars, output_path)

        if existing and existing.output_path and existing.output_path != output_path:
            remove_corpus_file(output_root, existing.output_path)
            idx.remove(existing.output_path)

        for part_fm, part_body, part_path in parts:
            part_content = wrap_with_front_matter(part_fm, part_body)
            write_corpus_file(output_root, part_path, part_content)

            idx.upsert(
                output_path=part_path,
                title=part_fm.title,
                body=part_body[:50000],
                source_type=source.source_type.value,
                source_name=source.name,
                source_key=change.source_key or "",
                source_url=change.source_url,
                updated_at=(
                    change.source_updated_at.isoformat() if change.source_updated_at else None
                ),
            )

        actual_output = parts[0][2] if parts else output_path

        obj = SourceObject(
            source_name=source.name,
            source_type=source.source_type,
            source_id=change.source_id,
            source_key=change.source_key,
            title=change.title,
            source_url=change.source_url,
            source_version=change.source_version,
            source_updated_at=change.source_updated_at,
            content_sha256=c_hash,
            output_path=actual_output,
            file_size=change.file_size,
            file_mtime=change.file_mtime,
            last_processed_at=now,
            last_error="stub:conversion_failed" if is_stub else None,
            retry_count=0 if not is_stub else ((existing.retry_count + 1) if existing else 1),
        )
        db.upsert_object(obj)


def _handle_delete(
    source: Source,
    change: DiscoveredChange,
    db: StateDB,
    idx: SearchIndex,
    output_root: Path,
) -> None:
    """Process a deletion. Thread-safe via _DB_WRITE_LOCK."""
    with _DB_WRITE_LOCK:
        existing = db.get_object(source.name, change.source_id)
        if existing and existing.output_path:
            remove_corpus_file(output_root, existing.output_path)
            idx.remove(existing.output_path)

            stem = Path(existing.output_path).stem
            parent = Path(existing.output_path).parent
            for part_file in (output_root / parent).glob(f"{stem}.part-*"):
                rel = str(part_file.relative_to(output_root))
                part_file.unlink(missing_ok=True)
                idx.remove(rel)

        db.delete_object(source.name, change.source_id)


def _reconcile_source(
    source: Source,
    db: StateDB,
    idx: SearchIndex,
    output_root: Path,
) -> None:
    """Reconcile a source: find and remove deleted objects."""
    try:
        current_ids = source.get_current_ids()
    except Exception:
        logger.error("Reconciliation failed for %s", source.name, exc_info=True)
        return

    stored_ids = db.get_all_source_ids(source.name)
    deleted_ids = stored_ids - current_ids

    for source_id in deleted_ids:
        logger.info("Reconciliation: deleting %s/%s", source.name, source_id)
        change = DiscoveredChange(
            source_id=source_id,
            action=ChangeAction.DELETE,
        )
        _handle_delete(source, change, db, idx, output_root)


def _convert_local_file(file_path: Path) -> str | None:
    """Convert a local file to Markdown using appropriate converter.

    PDF and Office conversions acquire a semaphore so at most 3 heavy
    conversions run at once — the remaining thread-pool threads stay free
    for fast I/O (downloads, text reads).
    """
    from workctx.normalise.convertibility import TEXT_EXTENSIONS
    from workctx.normalise.office import can_handle as office_handles
    from workctx.normalise.office import convert_office
    from workctx.normalise.pdf import can_handle as pdf_handles
    from workctx.normalise.pdf import convert_pdf

    if pdf_handles(file_path):
        with _HEAVY_CONVERSION_SEMAPHORE:
            return convert_pdf(file_path)

    if office_handles(file_path):
        with _HEAVY_CONVERSION_SEMAPHORE:
            return convert_office(file_path)

    suffix = file_path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        try:
            return file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    with _HEAVY_CONVERSION_SEMAPHORE:
        result = convert_office(file_path)
    if result:
        return result

    if _looks_like_text(file_path):
        try:
            return file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return None

    return None


def _looks_like_text(file_path: Path) -> bool:
    """Heuristic: read first 8KB and check if it looks like text."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
        if not chunk:
            return True
        if b"\x00" in chunk:
            return False
        try:
            chunk.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    except OSError:
        return False


def _make_unsupported_stub(change: DiscoveredChange) -> str:
    """Create a metadata-only stub for unsupported file types."""
    path = Path(change.local_path) if change.local_path else None
    lines = [
        f"# {change.title or change.source_id}",
        "",
        "This document could not be converted to Markdown.",
        "",
        f"- **File**: {change.source_id}",
    ]
    if path:
        lines.append(f"- **Type**: {path.suffix}")
        if change.file_size:
            lines.append(f"- **Size**: {change.file_size:,} bytes")
    if change.source_url:
        lines.append(f"- **Source**: {change.source_url}")
    return "\n".join(lines)


def _make_empty_stub(change: DiscoveredChange) -> str:
    """Create a minimal stub for pages with no retrievable content."""
    lines = [
        f"# {change.title or change.source_id}",
        "",
        "*This page had no retrievable content (restricted, draft, or empty).*",
        "",
    ]
    if change.source_url:
        lines.append(f"- **Source**: [{change.source_url}]({change.source_url})")
    if change.source_key:
        lines.append(f"- **Key**: {change.source_key}")
    return "\n".join(lines)


def _download_and_convert(source: Source, change: DiscoveredChange) -> str | None:
    """Download a file from SharePoint web source and convert to Markdown."""
    tmp_path = source.download_file(change.source_id)
    if not tmp_path:
        return None
    try:
        return _convert_local_file(tmp_path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _should_reconcile(checkpoint: SyncCheckpoint | None, days: int) -> bool:
    if not checkpoint or not checkpoint.last_reconciliation:
        return True
    elapsed = (datetime.now(UTC) - checkpoint.last_reconciliation).days
    return elapsed >= days
