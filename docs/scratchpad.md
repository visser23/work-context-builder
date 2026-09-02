# Scratchpad: Work Context Mirror

## Current Status
- Project initialised: 2026-09-01
- Final code review and polish: 2026-09-02
- 138 tests passing, 0 lint errors, consistent formatting
- Daemon running on macOS (launchd KeepAlive)
- Output + state on Servita OneDrive: /NHS England/DDTX/Context/

## Live Environment
- Confluence DC: `nhsd-confluence.digital.nhs.uk` -> spaces HOM, DTX (~1,292 pages)
- Jira DC: `nhsd-jira.digital.nhs.uk` -> projects HOTE, DTX (~3,328 issues)
- SharePoint: `nhs.sharepoint.com/sites/X26_Digital_Prevention_Service` -> browser mode (~3,500 docs)
- Telegram bot: `mv_nhs_sync_bot` -> chat ID 8337793696
- Git: `https://github.com/visser23/work-context-builder.git`

## Architecture
```
Sources             Auth                    Daemon Loop
--------            ----                    -----------
Confluence DC  -->  Bearer PAT         -->  Every 30s:
Jira DC        -->  Bearer PAT         -->    check if sync due (>24h)
SharePoint     -->  rtFa/FedAuth       -->    poll Telegram for commands
                    (cookie keep-alive)
                                            Sync Pipeline:
                                              discover_changes
                                              -> fetch/convert (60+ formats)
                                              -> write corpus
                                              -> index FTS5
                                              -> update state
                                              -> notify via Telegram

Service Management:
  macOS    -> launchd (KeepAlive, RunAtLoad)
  Linux    -> systemd user service
  Windows  -> Task Scheduler at-logon

Telegram Commands:
  /sync, /syncfull, /status, /help
```

## Authentication
- Atlassian DC: Personal Access Tokens via Bearer auth
- Atlassian Cloud: API tokens via Basic Auth (auto-detected)
- SharePoint: browser-based cookie capture via Playwright
  - rtFa + FedAuth cookies, keep-alive on each sync
  - Fallback to cached cookies on timeout/failure

## Lessons
- `fnmatch.fnmatch` treats `**/*` literally. Must strip `**/` prefix.
- Atlassian Cloud tokens do NOT work for Data Center.
- Confluence DC API uses `/rest/api` (no `/wiki` prefix).
- 30s timeout insufficient for Jira DC search — use 120s.
- Empty Confluence pages produce metadata stubs, not errors.
- SharePoint interactive login: poll for cookies instead of requiring Enter.
- MarkItDown `[all]` extra needed for full Office format support.
- Files with `last_error` must be re-attempted on next sync.
- `split_large_document` must preserve parent path to avoid filename collisions.
- SharePoint keepalive timeout should fall back to cached cookies, not fail.
- httpx logs can leak secrets in URLs — suppress at WARNING level.
- `__del__` is unreliable for cleanup — use explicit `close()` methods.
