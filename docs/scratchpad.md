# Scratchpad: Work Context Mirror

## Current Status
- All core phases complete
- 138 tests passing, 0 lint errors
- Background daemon with Telegram commands
- Cross-platform service management (launchd, systemd, Task Scheduler)
- First-run bootstrap scripts for macOS/Linux and Windows

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
