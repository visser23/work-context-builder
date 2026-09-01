# Requirements: Work Context Mirror

## User-Visible Behaviour

### Installation & Setup
- Install on macOS via `uv` / pip
- Create one YAML config per project
- Configure Confluence spaces, Jira projects, SharePoint directories
- Authenticate using existing credentials (API tokens in Keychain)
- Run `workctx doctor` to validate setup

### Daily Operation
- Run `workctx sync` for initial or incremental sync
- Schedule via `workctx install-schedule` (macOS launchd)
- Automatic delta detection — only changed content processed
- Telegram/macOS notifications on failure or recovery

### LLM Consumption
- Output directory contains clean Markdown with YAML front matter
- CONTEXT.md, AGENTS.md, CLAUDE.md provide LLM guidance
- SQLite FTS5 search via `workctx search`
- _meta/manifest.jsonl for programmatic access
- _meta/health.json for monitoring

## Sources

### Confluence
- Cloud and Data Center support
- CQL-based incremental sync via `lastmodified`
- Page-level granularity (one page = one Markdown file)
- 7-day reconciliation cycle for deletions
- Storage format → clean Markdown conversion

### Jira
- Cloud and Data Center support
- JQL-based incremental sync via `updated` field
- Issue-level granularity (one issue = one Markdown file)
- ADF → Markdown conversion
- Comments, field metadata, custom fields
- Configurable changelog/attachment inclusion

### SharePoint (OneDrive Local)
- Preferred: read local OneDrive-synced directory
- No Microsoft API/admin required
- Filesystem delta detection (mtime + size + hash)
- Files On-Demand materialisation support
- MarkItDown for Office docs, PyMuPDF4LLM for PDFs
- Optional: Graph API, rclone, Playwright fallbacks

## KPIs
- Initial sync: complete and correct
- Daily sync with no changes: seconds, near-zero API calls
- Daily sync with 5 changes: only those 5 objects processed
- Repeated sync: bit-identical corpus output (idempotent)

## Edge Cases
- Temporary Office files (~$*) ignored
- Cloud-only OneDrive placeholders materialised with retry
- Large documents (>300k chars) split deterministically at headings
- Unsupported file formats produce metadata-only stubs
- Authentication expiry: corpus preserved, alert sent
- Source outage: corpus preserved, alert sent
- Conversion failure: previous good version preserved
- Stale locks detected and recovered
