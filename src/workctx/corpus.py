"""Corpus output management: file writing, manifest, LLM instruction files."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from workctx.config import ProjectConfig
from workctx.models import ManifestEntry, SourceType
from workctx.normalise.common import (
    slugify,
)
from workctx.state import StateDB

logger = logging.getLogger(__name__)


def write_corpus_file(
    output_root: Path,
    relative_path: str,
    content: str,
) -> Path:
    """Write content to the corpus atomically (write to temp, then replace)."""
    target = output_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=".workctx_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(target))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise

    return target


def remove_corpus_file(output_root: Path, relative_path: str) -> None:
    """Remove a file from the corpus."""
    target = output_root / relative_path
    target.unlink(missing_ok=True)

    parent = target.parent
    while parent != output_root:
        try:
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break


def build_output_path(
    source_type: SourceType,
    source_name: str,
    source_id: str,
    *,
    source_key: str | None = None,
    title: str | None = None,
    space: str | None = None,
    project: str | None = None,
    relative_source_path: str | None = None,
) -> str:
    """Build the relative output path for a corpus file."""
    base = f"{source_type.value}/{source_name}"

    if source_type == SourceType.CONFLUENCE:
        slug = slugify(title or "untitled")
        subdir = f"{space}/" if space else ""
        return f"{base}/{subdir}{source_id}-{slug}.md"

    if source_type == SourceType.JIRA:
        key = source_key or source_id
        proj = project or "unknown"
        return f"{base}/{proj}/{key}.md"

    if source_type == SourceType.SHAREPOINT:
        if relative_source_path:
            return f"{base}/{relative_source_path}.md"
        slug = slugify(title or source_id)
        return f"{base}/{slug}.md"

    return f"{base}/{source_id}.md"


def generate_manifest(db: StateDB, output_root: Path) -> None:
    """Generate _meta/manifest.jsonl from the state database."""
    manifest_path = output_root / "_meta" / "manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    with open(manifest_path, "w", encoding="utf-8") as f:
        cursor = db.conn.execute(
            "SELECT * FROM source_objects "
            "WHERE output_path IS NOT NULL "
            "ORDER BY source_name, source_id"
        )
        for row in cursor:
            entry = ManifestEntry(
                source_type=row["source_type"],
                source_name=row["source_name"],
                source_id=row["source_id"],
                source_key=row["source_key"],
                output_path=row["output_path"],
                title=row["title"],
                source_url=row["source_url"],
                updated_at=_parse_dt(row["source_updated_at"]),
                synced_at=_parse_dt(row["last_processed_at"]),
                content_sha256=row["content_sha256"],
            )
            f.write(entry.model_dump_json() + "\n")


def generate_health(db: StateDB, output_root: Path, run_status: str) -> None:
    """Generate _meta/health.json."""
    health_path = output_root / "_meta" / "health.json"
    health_path.parent.mkdir(parents=True, exist_ok=True)

    sources_health: dict[str, dict] = {}
    cursor = db.conn.execute("SELECT * FROM sync_checkpoints")
    for row in cursor:
        sources_health[row["source_name"]] = {
            "status": "healthy" if row["last_success"] else "unknown",
            "last_success": row["last_success"],
        }

    health = {
        "last_run": datetime.now(UTC).isoformat(),
        "status": run_status,
        "sources": sources_health,
    }

    health_path.write_text(json.dumps(health, indent=2) + "\n")


def generate_index_md(config: ProjectConfig, db: StateDB, output_root: Path) -> None:
    """Generate _meta/INDEX.md."""
    index_path = output_root / "_meta" / "INDEX.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC).strftime("%d %b %Y %H:%M UTC")
    lines = [
        f"# {config.project.name}",
        "",
        f"Last sync: {now}",
        "",
        "## Sources",
        "",
    ]

    for src in config.sources.confluence:
        count = db.count_objects(src.name)
        lines.append(f"### Confluence: {src.name}")
        lines.append(f"- Spaces: {', '.join(src.spaces)}")
        lines.append(f"- Pages: {count:,}")
        lines.append("")

    for src in config.sources.jira:
        count = db.count_objects(src.name)
        lines.append(f"### Jira: {src.name}")
        lines.append(f"- Projects: {', '.join(src.projects)}")
        lines.append(f"- Issues: {count:,}")
        lines.append("")

    for src in config.sources.sharepoint:
        count = db.count_objects(src.name)
        lines.append(f"### SharePoint: {src.name}")
        lines.append(f"- Documents: {count:,}")
        lines.append("")

    index_path.write_text("\n".join(lines))


def generate_context_md(config: ProjectConfig, output_root: Path) -> None:
    """Generate CONTEXT.md — explains the corpus to humans and LLMs."""
    now = datetime.now(UTC).strftime("%d %b %Y %H:%M UTC")
    content = f"""# {config.project.name} — Project Context

This directory contains a machine-readable mirror of project knowledge
automatically synchronised by Work Context Mirror.

**Last updated:** {now}

## What this contains

This corpus mirrors content from the following sources:

"""

    for src in config.sources.confluence:
        content += f"- **Confluence** ({src.name}): spaces {', '.join(src.spaces)}\n"
    for src in config.sources.jira:
        content += f"- **Jira** ({src.name}): projects {', '.join(src.projects)}\n"
    for src in config.sources.sharepoint:
        content += f"- **SharePoint** ({src.name})\n"

    content += """
## How to use this corpus

Each file is a Markdown document with YAML front matter containing source
provenance (source type, source URL, timestamps, version information).

- `confluence/` — one file per Confluence page
- `jira/` — one file per Jira issue, plus **`SUMMARY.csv`** and **`SUMMARY.md`**
  (a single-file project overview similar to Jira's CSV export — ideal for
  Gantt charts, portfolio views, and quick status reports)
- `sharepoint/` — one file per converted document

## Timestamps

- `updated_at` — when the source was last modified upstream
- `synced_at` — when Work Context Mirror last processed this file
- `source_version` — the version number from the source system

## Provenance

Every file includes a `source_url` linking back to the original source.
When citing information, reference both the file path and the source URL.
"""

    write_corpus_file(output_root, "CONTEXT.md", content)


def generate_agents_md(config: ProjectConfig, output_root: Path) -> None:
    """Generate AGENTS.md — guidance for Codex-style agents."""
    content = f"""# {config.project.name} — Agent Instructions

You have access to a synchronised mirror of project knowledge.

## Before making project-specific assertions

1. Search the corpus using file paths, grep, or the index
2. For project status, timelines, or Gantt charts, start with `jira/*/SUMMARY.csv`
   or `jira/*/SUMMARY.md` — these contain all issues in a single tabular view
3. Prefer primary source documents over summaries
4. Consider timestamps — more recent content may supersede older content
5. Distinguish between decisions, proposals, drafts, and completed work
6. Identify conflicting information across sources

## When citing information

- Reference the file path within this corpus
- Include the original source URL from the YAML front matter
- Note the `updated_at` timestamp for recency context

## Important

- Do not invent project information when evidence is absent
- If information is ambiguous, say so and cite the conflicting sources
- The `_meta/manifest.jsonl` file lists all indexed documents
- The `_meta/health.json` file shows sync status
"""

    write_corpus_file(output_root, "AGENTS.md", content)


def generate_claude_md(config: ProjectConfig, output_root: Path) -> None:
    """Generate CLAUDE.md — guidance for Claude Code."""
    content = f"""# {config.project.name} — Claude Code Instructions

This directory contains automatically synchronised project knowledge from
Confluence, Jira, and SharePoint.

## Using this corpus

- Inspect files before making project-specific claims
- Each Markdown file has YAML front matter with `source_url`, `updated_at`,
  and other provenance fields
- **For project status or Gantt charts**, read `jira/*/SUMMARY.csv` or
  `jira/*/SUMMARY.md` first — one row per issue with key fields
- Prefer primary source documents (design docs, specs) over issue trackers
  for architectural decisions
- Consider timestamps: content may be outdated
- The `_meta/INDEX.md` provides an overview of all sources and counts

## Structure

- `confluence/` — wiki pages (one per file)
- `jira/` — issues with comments (one per file), plus `SUMMARY.csv`/`SUMMARY.md`
- `sharepoint/` — converted documents (one per file)
- `_meta/` — index, manifest, and health status

## Searching

Use ripgrep or the filesystem to find relevant content:
```
rg "supplier onboarding" confluence/ jira/
```

Or use the workctx search command if available:
```
workctx search "supplier onboarding"
```
"""

    write_corpus_file(output_root, "CLAUDE.md", content)


def generate_readme(config: ProjectConfig, output_root: Path) -> None:
    """Generate a top-level README.md."""
    content = f"""# {config.project.name}

Automatically synchronised project knowledge mirror.

See `CONTEXT.md` for details about this corpus.
See `AGENTS.md` or `CLAUDE.md` for AI agent guidance.
See `_meta/INDEX.md` for source overview and counts.

Generated by [Work Context Mirror](https://github.com/work-context-mirror).
"""

    write_corpus_file(output_root, "README.md", content)


def generate_jira_summary(config: ProjectConfig, db: StateDB, output_root: Path) -> None:
    """Generate per-source Jira summary files (CSV + Markdown table).

    Produces files similar to Jira's built-in CSV export, enabling quick
    project reports, Gantt charts, and portfolio views from a single file.
    """
    for jira_cfg in config.sources.jira:
        objects = db.get_objects_for_source(jira_cfg.name)
        jira_objects = [o for o in objects if o.source_type == SourceType.JIRA]
        if not jira_objects:
            continue

        rows = []
        for obj in sorted(jira_objects, key=lambda o: o.source_key or o.source_id):
            details = _extract_jira_details(output_root, obj.output_path)
            rows.append(
                {
                    "Key": obj.source_key or "",
                    "Summary": obj.title or "",
                    "Type": details.get("Type", ""),
                    "Status": details.get("Status", ""),
                    "Priority": details.get("Priority", ""),
                    "Resolution": details.get("Resolution", ""),
                    "Assignee": details.get("Assignee", ""),
                    "Reporter": details.get("Reporter", ""),
                    "Created": details.get("Created", ""),
                    "Updated": details.get("Updated", ""),
                    "Due Date": details.get("Due Date", details.get("Target end", "")),
                    "Labels": details.get("Labels", ""),
                    "Components": details.get("Components", ""),
                    "Fix Versions": details.get("Fix Versions", ""),
                    "Sprint": _clean_sprint(details.get("Sprint", "")),
                    "Epic": details.get("Epic", ""),
                    "Parent": details.get("Parent", ""),
                    "URL": obj.source_url or "",
                }
            )

        base_dir = f"jira/{jira_cfg.name}"
        _write_jira_csv(output_root, f"{base_dir}/SUMMARY.csv", rows)
        _write_jira_md(output_root, f"{base_dir}/SUMMARY.md", rows, jira_cfg.name)
        logger.info("Jira/%s: summary generated (%d issues)", jira_cfg.name, len(rows))


_DETAIL_RE = re.compile(r"^- \*\*(.+?)\*\*:\s*(.+)$")


def _extract_jira_details(output_root: Path, output_path: str | None) -> dict[str, str]:
    """Parse the ## Details and ## Custom Fields sections from a Jira Markdown file."""
    if not output_path:
        return {}
    full_path = output_root / output_path
    if not full_path.exists():
        return {}

    details: dict[str, str] = {}
    in_section = False
    try:
        for line in full_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## Details") or line.startswith("## Custom Fields"):
                in_section = True
                continue
            if line.startswith("## ") and in_section:
                in_section = False
                continue
            if in_section:
                m = _DETAIL_RE.match(line)
                if m:
                    details[m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    return details


def _clean_sprint(raw: str) -> str:
    """Extract the sprint name from Jira's verbose Sprint representation."""
    m = re.search(r"name=([^,\]]+)", raw)
    return m.group(1).strip() if m else raw.split("[")[0].strip() if raw else ""


def _write_jira_csv(output_root: Path, relative_path: str, rows: list[dict[str, str]]) -> None:
    """Write a CSV summary of Jira issues."""
    if not rows:
        return
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    write_corpus_file(output_root, relative_path, buf.getvalue())


def _write_jira_md(
    output_root: Path, relative_path: str, rows: list[dict[str, str]], source_name: str
) -> None:
    """Write a Markdown table summary of Jira issues."""
    if not rows:
        return

    cols = ["Key", "Summary", "Type", "Status", "Priority", "Assignee", "Sprint", "Epic", "Updated"]
    lines = [
        f"# Jira Project Summary — {source_name}",
        "",
        f"*{len(rows)} issues as of {datetime.now(UTC).strftime('%d %b %Y %H:%M UTC')}*",
        "",
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in rows:
        cells = [_md_escape(row.get(c, "")) for c in cols]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("*Full CSV export available at `SUMMARY.csv` in this directory.*")
    lines.append("")
    write_corpus_file(output_root, relative_path, "\n".join(lines))


def _md_escape(text: str) -> str:
    """Escape pipe characters for Markdown tables and truncate long values."""
    text = text.replace("|", "\\|").replace("\n", " ")
    if len(text) > 80:
        text = text[:77] + "..."
    return text


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None
