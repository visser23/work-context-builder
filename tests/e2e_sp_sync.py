"""Small-scope sync: SharePoint only, non-recursive, with progress.

Lists files in ONE folder (the configured server_relative_path),
downloads and converts up to --limit files. Writes to corpus + DB.

Usage:
    uv run python tests/e2e_sp_sync.py [--limit N] [--recurse]
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="SharePoint-only sync test")
    parser.add_argument("--limit", type=int, default=10, help="Max files to process")
    parser.add_argument("--recurse", action="store_true", help="Recurse subfolders")
    args = parser.parse_args()

    from workctx.config import load_config
    from workctx.corpus import build_output_path, write_corpus_file
    from workctx.models import FrontMatter, SourceObject, SourceType
    from workctx.normalise.common import content_hash, wrap_with_front_matter
    from workctx.sources.sharepoint_web import SharePointWebSource
    from workctx.state import StateDB
    from workctx.sync import _download_and_convert, _make_empty_stub

    cfg = load_config("workctx.yaml")
    sp_configs = [s for s in cfg.sources.sharepoint if s.mode == "browser"]
    if not sp_configs:
        print("No browser-mode SharePoint sources in config")
        sys.exit(1)

    sp_config = sp_configs[0]
    source = SharePointWebSource(sp_config)

    site_url = (sp_config.site_url or "").rstrip("/")
    server_path = (
        sp_config.server_relative_path
        or source._default_server_relative_path()
    )

    db = StateDB(cfg.state_dir / "state.sqlite")
    output_root = cfg.output_root_path

    print(f"Source: {source.name}")
    print(f"Path:   {server_path}")
    print(f"Limit:  {args.limit} files")
    print(f"Recurse: {args.recurse}")
    print(flush=True)

    # Step 1: Get client (handles keep-alive)
    print("Getting client (cookie keep-alive)...", flush=True)
    client = source._get_client()
    print("OK", flush=True)

    # Step 2: List files in target folder only
    print(f"\nListing files in: {server_path}", flush=True)
    t0 = time.monotonic()


    all_files: list[dict] = []

    def list_folder(folder_path: str, depth: int = 0) -> None:
        indent = "  " * depth
        resp = source._sp_get_by_path(
            client, "GetFolderByServerRelativeUrl", folder_path,
            suffix="/Files",
            params={"$select": "Name,ServerRelativeUrl,TimeLastModified,Length,UniqueId"},
        )
        if resp and resp.status_code == 200:
            files = resp.json().get("d", {}).get("results", [])
            all_files.extend(files)
            print(f"{indent}  {folder_path.split('/')[-1]}: {len(files)} files", flush=True)

        if not args.recurse or len(all_files) >= args.limit:
            return

        resp2 = source._sp_get_by_path(
            client, "GetFolderByServerRelativeUrl", folder_path,
            suffix="/Folders",
            params={"$select": "Name,ServerRelativeUrl"},
        )
        if resp2 and resp2.status_code == 200:
            folders = resp2.json().get("d", {}).get("results", [])
            for f in folders:
                name = f.get("Name", "")
                if name.startswith("_") or name == "Forms":
                    continue
                sub_path = f.get("ServerRelativeUrl", "")
                if sub_path and len(all_files) < args.limit:
                    list_folder(sub_path, depth + 1)

    list_folder(server_path)
    elapsed = time.monotonic() - t0
    print(f"\nFound {len(all_files)} files in {elapsed:.1f}s", flush=True)

    if not all_files:
        print("No files found. Trying subfolders one level...")
        resp = source._sp_get_by_path(
            client, "GetFolderByServerRelativeUrl", server_path,
            suffix="/Folders",
            params={"$select": "Name,ServerRelativeUrl"},
        )
        if resp and resp.status_code == 200:
            folders = resp.json().get("d", {}).get("results", [])
            print(f"Found {len(folders)} subfolders:")
            for f in folders[:10]:
                print(f"  {f.get('Name')}")
            if folders:
                first_sub = folders[0].get("ServerRelativeUrl", "")
                list_folder(first_sub)
                print(f"Found {len(all_files)} files in first subfolder")

    if not all_files:
        print("No files found anywhere")
        sys.exit(1)

    # Step 3: Process files
    to_process = all_files[:args.limit]
    print(f"\nProcessing {len(to_process)} files:", flush=True)

    succeeded = 0
    stubs = 0

    for i, file_data in enumerate(to_process, 1):
        fname = file_data.get("Name", "?")
        furl = file_data.get("ServerRelativeUrl", "")
        fsize = file_data.get("Length", "?")

        print(f"\n  [{i}/{len(to_process)}] {fname}", flush=True)
        print(f"    URL:  {furl}", flush=True)
        print(f"    Size: {fsize} bytes", flush=True)

        from workctx.models import ChangeAction, DiscoveredChange

        change = DiscoveredChange(
            source_id=furl,
            source_key=file_data.get("UniqueId", furl),
            title=Path(fname).stem,
            source_url=f"{site_url}{furl}",
            source_version=file_data.get("TimeLastModified", ""),
            action=ChangeAction.ADD,
            file_size=int(fsize) if fsize and fsize != "?" else None,
            metadata={"library": sp_config.doc_library},
        )

        body_md = _download_and_convert(source, change)
        is_stub = False

        if not body_md:
            body_md = _make_empty_stub(change)
            is_stub = True

        output_path = build_output_path(
            SourceType.SHAREPOINT,
            source.name,
            change.source_id,
            title=change.title,
            relative_source_path=change.source_id,
        )

        now = datetime.now(UTC)
        fm = FrontMatter(
            source_type="sharepoint",
            source_name=source.name,
            source_id=change.source_id,
            title=change.title or change.source_id,
            source_url=change.source_url,
            source_key=change.source_key,
            source_path=change.source_id,
            source_version=change.source_version,
            updated_at=change.source_updated_at,
            synced_at=now,
        )

        full_content = wrap_with_front_matter(fm, body_md)
        c_hash = content_hash(full_content)

        write_corpus_file(output_root, output_path, full_content)
        print(f"    Written: {output_path}", flush=True)

        obj = SourceObject(
            source_name=source.name,
            source_type=SourceType.SHAREPOINT,
            source_id=change.source_id,
            source_key=change.source_key,
            title=change.title,
            source_url=change.source_url,
            source_version=change.source_version,
            source_updated_at=change.source_updated_at,
            content_sha256=c_hash,
            output_path=output_path,
            file_size=change.file_size,
            last_processed_at=now,
            last_error="stub:conversion_failed" if is_stub else None,
            retry_count=1 if is_stub else 0,
        )
        db.upsert_object(obj)

        if is_stub:
            stubs += 1
            print(f"    Result: STUB ({len(body_md)} chars)", flush=True)
        else:
            succeeded += 1
            print(f"    Result: OK ({len(body_md):,} chars)", flush=True)

    db.close()

    print(f"\n{'='*60}")
    print(f"Results: {succeeded} converted, {stubs} stubs")
    print(f"Total files found on SharePoint: {len(all_files)}")


if __name__ == "__main__":
    main()
