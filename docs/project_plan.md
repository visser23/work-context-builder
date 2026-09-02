# Project Plan: Work Context Mirror

## Phase 1: Foundation
- [x] Repository initialisation (git, directories)
- [x] docs/ planning files
- [x] pyproject.toml with all dependencies
- [x] .gitignore
- [x] Pydantic configuration models (config.py)
- [x] Domain models (models.py)
- [x] SQLite state database with migrations (state.py)
- [x] macOS Keychain integration (secrets.py)
- [x] Logging framework (logging_config.py)
- [x] CLI skeleton with Click (cli.py)
- [x] Execution locking (locking.py)
- [x] Unit tests for Phase 1

## Phase 2: SharePoint Local Vertical Slice
- [x] SharePoint source adapter — onedrive_local mode (sources/sharepoint.py)
- [x] Filesystem delta detection (mtime + size → hash on change)
- [x] Files On-Demand materialisation with retry
- [x] MarkItDown integration (normalise/office.py)
- [x] PyMuPDF4LLM integration (normalise/pdf.py)
- [x] Common normalisation: front matter, splitting (normalise/common.py)
- [x] Corpus output writing with atomic replace (corpus.py)
- [x] Manifest generation (_meta/manifest.jsonl)
- [x] Sync orchestration for SharePoint (sync.py)
- [x] End-to-end: workctx sync processes local SharePoint folder
- [x] Unit + integration tests for Phase 2

## Phase 3: Jira
- [x] Jira source adapter (sources/jira.py)
- [x] Jira Cloud authentication (API token)
- [x] JQL incremental query with checkpoint
- [x] Field metadata retrieval
- [x] ADF → Markdown conversion (normalise/atlassian.py)
- [x] Comment pagination and rendering
- [x] Issue Markdown generation with front matter
- [x] Jira reconciliation for deletions
- [x] Unit + integration tests for Phase 3

## Phase 4: Confluence
- [x] Confluence source adapter (sources/confluence.py)
- [x] Confluence Cloud authentication (API token)
- [x] CQL incremental query with checkpoint
- [x] Confluence storage format → Markdown (normalise/atlassian.py)
- [x] Page Markdown generation with front matter
- [x] Confluence reconciliation for deletions
- [x] Unit + integration tests for Phase 4

## Phase 5: Search & LLM Integration
- [x] SQLite FTS5 indexing (indexing.py)
- [x] workctx search CLI command
- [x] CONTEXT.md generation
- [x] AGENTS.md generation
- [x] CLAUDE.md generation
- [x] _meta/INDEX.md generation
- [x] _meta/health.json generation
- [x] Tests for Phase 5

## Phase 6: Operations
- [x] launchd scheduler (scheduler.py)
- [x] workctx install-schedule / remove-schedule / schedule-status
- [x] Telegram notifications (notifications.py)
- [x] macOS Notification Center
- [x] Alert suppression (dedup identical failures)
- [x] Recovery notifications
- [x] workctx doctor (comprehensive validation)
- [x] workctx status
- [x] workctx init (interactive config generation)
- [x] Tests for Phase 6

## Phase 7: Extended Modes
- [x] Atlassian Data Center support (auto-detection, PAT auth)
- [x] Playwright SharePoint browser mode (cookie capture + REST API)

## Phase 8: Background Daemon
- [x] Background daemon with daily sync
- [x] Telegram command polling (/sync, /syncfull, /status, /help)
- [x] Cross-platform service management (launchd, systemd, Task Scheduler)
- [x] Local folder source adapter
- [x] Rich progress display with ETA
- [x] Skip-before-download for unconvertible files
- [x] First-run bootstrap scripts (setup.sh, setup.ps1)

## Phase 9: Reliability & Polish
- [x] SharePoint ChangeToken persisted in checkpoint metadata (not last_checkpoint)
- [x] Version-only changes update DB version without file rewrite
- [x] Cookie cache-first strategy (HTTP test before Playwright launch)
- [x] Daemon cookie keepalive every 4h with Telegram expiry notifications
- [x] Fixed GetChanges Edm.Int64 error (removed FetchLimit parameter)
- [x] doc_library config correctly distinguishes list title from folder path

## Phase 10: Jira Summary & Reporting
- [x] Jira SUMMARY.csv generation (per source, CSV export style)
- [x] Jira SUMMARY.md generation (Markdown table format)
- [x] Integration into sync pipeline
- [x] Automated tests for extraction, cleaning, escaping, and generation

## Phase 11: LLM Project Integration & Code Quality
- [x] PROJECT_BRIEF.md — single-file overview for ChatGPT/Claude project uploads
- [x] CHATGPT_INSTRUCTIONS.md — ready-to-paste project instructions
- [x] Enhanced CLAUDE.md following 2026 best practices (concise, structured)
- [x] Local folder sources included in all generated context files
- [x] Checkpoint safety: never advance past failed objects
- [x] Resource cleanup: DB/index/adapter close() in finally blocks
- [x] Removed unimplemented modes (graph, rclone, device_code)
- [x] Removed dead code (html.py converter, ChangeAction.RENAME)
- [x] Code review and quality improvements
- [x] 151 tests passing, 0 lint errors

## Documentation
- [x] README.md (installation → first sync)
- [x] Example configuration file
- [x] Authentication setup guide (in README)
- [x] SharePoint/OneDrive setup instructions (in README)
- [x] Telegram setup instructions (in README)
- [x] Troubleshooting guide (in README)
- [x] AI-assistant configuration prompt template
- [x] "Using with AI Assistants" section in README (ChatGPT, Claude, Cursor/Codex)
