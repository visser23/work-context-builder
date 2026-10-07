# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [1.2.0]

### Added
- `include_invited` (default **on**) for Teams transcripts: pulls the
  transcripts of *every* meeting you were invited to, not only ones you
  organised or that were shared with you directly. It uses the security-trimmed
  tenant-wide recording search and keeps recordings in other people's OneDrives,
  Teams team sites (`/sites/msteams_*`) and sites where you are a contributor.
- `include_all_sites` (default off) to also include read-only sites such as
  organisation-wide webinars.

### Fixed
- Docs said `exclude_titles` matched substrings; it is a case-insensitive glob
  (use `"*1:1*"`). Examples corrected.
- A transient failure of the site-permission check aborts the run (and the
  reconcile) rather than silently dropping meetings.

## [1.1.0]

### Added
- **Teams meeting transcripts source** (`sources.transcripts`). Finds the
  recordings of meetings you organised or that were shared with you via
  SharePoint Search and downloads each transcript through SharePoint's media
  API, reusing the existing browser-mode SharePoint login (no app
  registration). Output: `transcripts/<source>/<year>/<date>-<title>-<id>.md`
  with `meeting_date`, `duration_minutes` and `participants` front matter and
  `[HH:MM:SS] **Speaker:** text` turns.
- Automatic, silent acquisition of OneDrive-host (`<tenant>-my.sharepoint.com`)
  cookies from the existing Playwright profile.
- `workctx sync --source/-s <name>` to sync only selected sources.
- `workctx doctor` checks for transcripts sources.
- Incremental sync for transcripts: unchanged old recordings are skipped,
  recent ones are always rechecked (transcripts arrive after the meeting),
  failed items are retried.
- Generated `CONTEXT.md`, `AGENTS.md`, `CLAUDE.md`, `PROJECT_BRIEF.md`,
  `CHATGPT_INSTRUCTIONS.md` and `README.md` now mention transcripts.

### Changed
- Full-text index body cap raised from 50,000 to 400,000 characters so long
  meeting transcripts are fully searchable.
- Version numbers in `pyproject.toml` and the package are now in sync.

### Security
- Session cookies are only sent to the configured tenant's two SharePoint
  hosts (strict https/host/port validation; foreign search hits are dropped).
- KQL search values are escaped; transcript titles are sanitised for paths.

## [1.0.0]

- Initial public release: Confluence, Jira, SharePoint (OneDrive sync and
  browser mode) and local-folder sources, SQLite FTS5 search, background
  daemon, Telegram/macOS notifications.
