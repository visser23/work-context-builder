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

### Teams Meeting Transcripts
- Reuses a browser-mode SharePoint login — no app registration or admin consent
- Discovers recordings (`Media.Meeting`) for every meeting the user was invited
  to: own OneDrive, other people's OneDrives, Teams team sites and sites where
  the user is a contributor (read-only sites opt-in); optional extra sites
- One meeting = one Markdown file in `transcripts/<source>/<year>/`
- Speaker-attributed, timestamped turns; front matter has meeting date,
  duration and participants
- Late-arriving transcripts picked up (recent meetings rechecked every run)
- Filters: `since_days`, `exclude_titles` (globs), `include_own`, `include_shared`,
  `include_invited`, `include_all_sites`
- `workctx sync --source <name>` syncs a single source
- Long transcripts are fully searchable (index body cap 400k chars)
- Inaccessible recordings (locked/leaver OneDrives) skipped quietly, never fatal
- Never mass-deletes on a partial enumeration (reconcile aborts on errors)

### Email, Calendar and Slack (v1.3.0)
- Reuse the persistent browser profile's SSO session — no app registration, API
  token or admin consent; credentials held in memory only, never persisted/logged
- Email: chosen folders (default inbox + sent), rolling `since_days` window, one
  file per message; quoted history trimmed; sender/subject exclusion globs
- Calendar: `past_days`/`future_days` window, one file per event occurrence;
  change key drives updates; cancelled events removed
- Slack: one digest per conversation per UTC day (channels, private, DMs, group
  DMs, threads); Enterprise Grid supported by reading each workspace
- Strictly read-only (GET / read-only Slack methods); tokens sent only to the
  configured https host
- Aged-out items are retained; items deleted inside the window are reconciled;
  Slack never deletes
- `workctx auth login-web --source <name>` for the rare human sign-in; `doctor`
  checks every source; silent SSO re-login when the session lapses

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
