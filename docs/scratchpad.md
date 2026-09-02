# Scratchpad: Work Context Mirror

## Current Status
- All core phases complete
- 138 tests passing, 0 lint errors
- Background daemon with Telegram commands (launchd KeepAlive on macOS)
- Cross-platform service management (launchd, systemd, Task Scheduler)
- First-run bootstrap scripts for macOS/Linux and Windows
- SharePoint incremental delta via GetChanges API (ChangeToken persisted in checkpoint metadata)
- Cookie keepalive: daemon pings SharePoint every 4h via HTTP, notifies via Telegram on expiry
- Dedup: uses (source_name, source_id) UNIQUE constraint + source_version + content_sha256
- Version-only changes (same content hash) now update source_version in DB without file rewrite

## Architecture Notes
- SharePoint list name may differ per tenant: "Documents" vs "Shared Documents" — `doc_library` in config
- GetChanges API: `FetchLimit` parameter causes `Edm.Int64` errors on some SP tenants — removed
- ChangeToken stored in `sync_checkpoints.metadata` (JSON), not `last_checkpoint` (ISO timestamp)
- Cookie validation: cache-first strategy — lightweight HTTP GET before Playwright launch
- Playwright uses `wait_until="domcontentloaded"` (not `networkidle`) to avoid SP background request timeouts

## Lessons
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
