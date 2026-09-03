# Scratchpad: Work Context Mirror

## Current Status
- All core phases complete + LLM integration polish + external review hardening
- 184 tests passing, 0 lint errors
- Background daemon with Telegram commands (launchd KeepAlive on macOS)
- Cross-platform service management (launchd, systemd, Task Scheduler)
- First-run bootstrap scripts for macOS/Linux and Windows
- SharePoint incremental delta via GetChanges API (ChangeToken persisted in checkpoint metadata)
- Cookie keepalive: daemon pings SharePoint every 4h via HTTP, notifies via Telegram on expiry
- Dedup: uses (source_name, source_id) UNIQUE constraint + source_version + content_sha256
- Version-only changes (same content hash) now update source_version in DB without file rewrite
- LLM-optimised output: PROJECT_BRIEF.md, CHATGPT_INSTRUCTIONS.md, CLAUDE.md, AGENTS.md
- Jira summary: SUMMARY.csv + SUMMARY.md per source for Gantt/status views

## Architecture Notes
- SharePoint list name may differ per tenant: "Documents" vs "Shared Documents" — `doc_library` in config
- GetChanges API: `FetchLimit` parameter causes `Edm.Int64` errors on some SP tenants — removed
- ChangeToken stored in `sync_checkpoints.metadata` (JSON), not `last_checkpoint` (ISO timestamp)
- Cookie validation: cache-first strategy — lightweight HTTP GET before Playwright launch
- Playwright uses `wait_until="domcontentloaded"` (not `networkidle`) to avoid SP background request timeouts
- Checkpoint safety: never advance past failed objects (tracks earliest failure timestamp)
- Resource cleanup: DB/index/adapters closed in `finally` blocks even on exception
- SharePoint modes restricted to `onedrive_local` and `browser` (graph/rclone removed as unimplemented)
- Auth modes restricted to `api_token`, `pat`, `basic`, `browser` (device_code removed as unimplemented)

## Lessons
- External review: prioritise by actual threat model (local CLI ≠ SaaS), not OWASP severity theatre.
- SP deletion detection: never use substring matching for identity lookups — persist proper IDs.
- Daemon freshness: use stalest (min) source, not freshest (max), to trigger daily sync.
- Multipart cleanup: always remove ALL old parts before writing new ones to avoid corpus zombies.
- Default-deny for file extensions: unknown types should be rejected, not optimistically converted.
- Trust boundary disclaimers in LLM instruction files cost nothing and are responsible framing.
- Schema migrations: SQLite ALTER TABLE ADD COLUMN + COALESCE in upsert preserves existing data cleanly.
- `fnmatch.fnmatch` treats `**/*` literally. Must strip `**/` prefix.
- Atlassian Cloud tokens do NOT work for Data Center instances.
- Confluence DC API uses `/rest/api` (no `/wiki` prefix).
- 30s timeout insufficient for large Jira instances — use 120s.
- Empty Confluence pages should produce metadata stubs, not errors.
- SharePoint interactive login: poll for cookies instead of requiring Enter.
- MarkItDown `[all]` extra needed for full Office format support.
- Files with `last_error` must be re-attempted on next sync.
- `split_large_document` must preserve parent path to avoid filename collisions.
- SharePoint keepalive timeout should fall back to cached cookies, not fail.
- httpx logs can leak secrets in URLs — suppress at WARNING level.
- `__del__` is unreliable for cleanup — use explicit `close()` methods.
- SP GetChanges `FetchLimit` int param causes Edm.Int64 OData error — just omit it.
- ChangeToken must be stored separately from `last_checkpoint` (which holds ISO timestamps).
- When content hash matches but version differs, still update version in DB to prevent repeated re-fetches.
- Always `uv sync` after code changes before testing via `uv run` to ensure latest build.
- ChatGPT Projects: 5-40 file limit → single PROJECT_BRIEF.md critical for quick context
- Claude Projects: RAG handles large corpora, CLAUDE.md should be concise (<200 lines)
- Dead code removal: html.py converter was unused (HTML goes through MarkItDown), ChangeAction.RENAME never referenced
