# Scratchpad: Work Context Mirror

## Current Status
- Project initialised: 2026-09-01
- All core phases complete (sources, sync, normalisation, SharePoint browser mode)
- Background daemon with Telegram commands implemented
- 105 tests passing, 0 lint errors
- CLI operational: `uv run workctx --version` confirms 0.1.0
- Doctor passes: 18/18 checks against NHS DC instances
- First full sync complete (Jira DC + Confluence DC) — 4600+ Markdown files
- SharePoint browser mode with keep-alive and delta detection
- 60+ file format support (Office, PDF, code, config, markup, etc.)
- Cross-platform service management (launchd, systemd, Task Scheduler)
- First-run bootstrap scripts for macOS/Linux and Windows

## Live Environment
- Confluence DC: `nhsd-confluence.digital.nhs.uk` -> spaces HOM, DTX (~1291 pages)
- Jira DC: `nhsd-jira.digital.nhs.uk` -> projects HOTE, DTX (~1800 issues)
- SharePoint: `nhs.sharepoint.com/sites/X26_Digital_Prevention_Service` -> browser mode
- Telegram bot: `mv_nhs_sync_bot` -> chat ID 8337793696, notifications + commands
- Git: committed to `main`, remote `https://github.com/visser23/work-context-builder.git`

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
                                              -> fetch content
                                              -> normalise to MD (60+ formats)
                                              -> write corpus
                                              -> index FTS5
                                              -> update state
                                              -> notify via Telegram

Service Management:
  macOS    -> launchd (KeepAlive, RunAtLoad)
  Linux    -> systemd user service
  Windows  -> Task Scheduler at-logon

Telegram Commands:
  /sync      -> trigger incremental sync
  /syncfull  -> trigger full sync
  /status    -> show last sync times + counts
  /help      -> list commands
```

## Build Summary
- Python 3.13 via uv
- 61+ dependencies resolved (+ Playwright for SP browser mode)
- All core modules implemented
- Cross-platform: macOS, Windows, Linux

## Authentication
- Atlassian Cloud: API tokens via Basic Auth (username + token)
- Atlassian Data Center: Personal Access Tokens via Bearer auth
- Auto-detection: probes `/rest/api/2/myself` with Bearer, falls back to Cloud
- NHS instances confirmed as Data Center (not Cloud)
- DC uses API v2 (not v3), no `/wiki` prefix for Confluence
- SharePoint: browser-based cookie capture via Playwright
  - rtFa + FedAuth cookies captured during interactive login
  - Keep-alive: headless Playwright refreshes cookies on each sync
  - Cookies stored in OS credential store
  - Delta detection via SharePoint GetChanges API with ChangeToken

## File Type Coverage
- Office: .docx, .doc, .pptx, .ppt, .xlsx, .xls, .xlsm, .xlsb, .rtf
- PDF: .pdf (via PyMuPDF4LLM)
- Email: .msg, .eml
- Web: .html, .htm, .mhtml, .mht
- Data: .csv, .tsv, .json, .jsonl, .xml, .epub, .ipynb
- Archives: .zip
- Code: 50+ languages (py, js, ts, java, go, rs, c, cpp, cs, rb, php, etc.)
- Markup: .md, .rst, .adoc, .tex, .wiki
- Config: .yaml, .toml, .ini, .env, .tf, .hcl, .dockerfile, etc.
- Unknown text files auto-detected via heuristic
- Catch-all MarkItDown fallback before stubbing

## Key Findings
- Jira project key for HomeTest is `HOTE` (not `HOM`)
- NHS Confluence API base is `/rest/api` (no `/wiki` prefix for DC)
- NHS SharePoint not synced via OneDrive locally — browser mode required
- Confluence DC returns empty `body.storage.value` for ~12% of pages
- Jira DC initial full sync is slow (~30+ min for 1800 issues)
- SharePoint REST API accepts rtFa/FedAuth cookies (no app registration)

## Lessons
- `fnmatch.fnmatch` treats `**/*` literally. Must strip `**/` prefix.
- ruff `--fix` handles import sorting but E501 requires manual breaking.
- Atlassian Cloud tokens (ATATT3x...) do NOT work for Data Center.
- Confluence DC API does not use `/wiki/rest/api` prefix.
- 30s timeout is insufficient for Jira DC search — use 120s.
- Empty Confluence pages should produce metadata stubs, not errors.
- SharePoint interactive login: poll for cookies instead of requiring Enter.
- Playwright persistent context preserves browser state between runs.
- MarkItDown `[all]` extra needed for full Office format support.
- Files with `last_error` set must be re-attempted on next sync.
