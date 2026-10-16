# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [1.4.0]

### Fixed
- **Updated multi-part documents left orphans behind.** Large documents are stored as
  `name.part-001.md`, `name.part-002.md`, …. On update the old cleanup globbed the stored
  path (`name.part-001`), which never matched its own siblings, so a document that shrank
  or was renamed kept dead `part-NNN` files forever (and stale index rows). Cleanup now
  lists the document's actual files literally (`corpus.document_files`), removes every
  part that is no longer produced, and handles delete the same way. An edited file always
  overwrites its original; nothing new is added next to it.
- Stale single-file twins of documents that are now split are pruned on every run
  (`_prune_stale_twins`).
- Files that are now excluded or unconvertible on the server (e.g. `.emf`, `.zip`) have
  their old stub removed instead of lingering.
- **Jira SUMMARY re-read every issue file on every sync** (1h20m when OneDrive had
  evicted the files, all while holding the run lock). Parsed details are now cached in
  SQLite (`jira_details`, schema v3) keyed by content hash; only changed issues are read.

### Added
- More documents become searchable text (nothing is converted if it would not make sense
  as text, e.g. images, video, CAD, binaries):
  - legacy Office/RTF/WordPerfect/xlsb/ODF-presentation via LibreOffice headless when
    installed (`.doc .dot .rtf .wpd .xls .xlsb .ppt .pptm .ppsx .potx .odp …`);
  - pure-Python readers for OpenDocument (`.odt .ods .odp`) and draw.io diagrams;
  - heavy/oversized `.pptx` fall back to slide text only (images ignored);
  - scanned/image-only PDFs are OCR'd (macOS Vision via `pip install .[ocr]`, or
    `tesseract` on PATH); OCR output is labelled as such;
  - `.vtt`/`.srt` captions are treated as text.
- Stubs are retried once after a converter upgrade (`CONVERTER_REVISION`).

## [1.3.2]

### Fixed
- **SharePoint never picked up new or changed files after the first sync.** Change
  detection used the `GetChanges` feed and resolved each change with a field
  (`File_x0020_Size`) the tenant rejects (HTTP 400); the error was swallowed, every
  change was dropped and the delta token advanced anyway. Folders created, renamed or
  moved on the server (e.g. `4. Remote Care`) therefore never appeared, and
  edits were never refreshed. Discovery is now a full enumerate-and-diff on every run
  (about a minute for ~5,000 files), which cannot lose events. Folder renames, moves,
  restores and files added in bulk are all picked up.
- A single connection reset / 429 / 5xx no longer fails the whole SharePoint source:
  every request retries with back-off (honouring `Retry-After`) and downloads retry too.
  A listing that could not be read completely now *fails the source loudly* instead of
  returning a partial list (which used to cause mass deletions), and nothing is
  deleted from an incomplete or implausible listing (>50% of stored files "gone" needs
  `sync --full` / `reconcile --force`).
- **"Session expired" false alarms.** A throttled/failed probe was treated as a rejected
  login, triggering needless browser refreshes and alerts. Cookies are now only judged
  dead on a 401/403/redirect; transient failures are retried and reported as
  "unreachable" (no refresh, no alert). Alerts now state the real cause, a crashed
  refresh is no longer reported as an expired session, and `doctor` says the same.
  A 401/403/redirect during a sync is now double-checked with the probe before the
  login is declared dead; otherwise it is retried like any transient fault (previously
  several worker threads could burn the whole refresh budget on one bad second).
- A live sync's lock was treated as stale after one hour, so a long first sync could have
  its lock stolen by the daemon (`database is locked`). Live processes keep the lock for
  up to 12 hours.
- Files that fail to convert are retried a few runs (3), then only when they change.

### Changed
- **Shorter corpus layout:** SharePoint documents now live directly under
  `sharepoint/<source>/` (previously `sharepoint/<source>/sites/<site>/<library>/<root
  folder>/...`, which also forced long paths to be clamped into mangled folder names).
  Existing corpora are migrated automatically on the next sync (rename only, no
  re-download); preview with `workctx relayout --dry-run`.
- Sync alerts include the underlying cause per failed source.

## [1.3.1]

### Fixed
- Enterprise Grid: DMs, group DMs and shared channels are listed by every
  workspace and were written once per workspace (duplicate digests). Each
  conversation is now read once from a single owning workspace; digests already
  stored under another workspace are removed automatically.

- Outlook throttling (HTTP 429) during a large back-fill: one throttled request
  now pauses every worker thread (shared back-off) and the retry budget is 8
  attempts, so a 12,000-message import no longer drops items (5 had failed).
- Read timeouts / connection errors talking to Outlook are retried with back-off
  (a year-long calendar query timed out once and failed the whole source).

### Changed
- Larger default windows (mail 90 days, calendar 90 days back, Slack 30 days) and
  a 50,000-message per-folder cap (was 5,000, which would have truncated large
  inboxes); mail listing uses 200-item pages. Set `since_days: 3650` (mail) or a
  large Slack `since_days` to back-fill the whole history.

## [1.3.0]

### Added
- **Email source** (`sources.mail`): Outlook / Exchange Online messages from
  chosen folders (default inbox + sent items), one Markdown file per message
  under `email/<source>/<YYYY>/<MM>/`, with sender, folder and participants
  front matter, quoted-reply trimming, attachment names and sender/subject
  exclusion globs.
- **Calendar source** (`sources.calendar`): past and upcoming events (including
  each recurring occurrence) under `calendar/<source>/<YYYY>/<MM>/`, with time,
  location, organiser, attendee responses and the online-meeting link;
  changes are detected via the event change key, cancelled events are removed.
- **Slack source** (`sources.slack`): one digest per conversation per UTC day
  under `slack/<source>/<workspace>/<conversation>/`, covering public/private
  channels, DMs and group DMs with thread replies, resolved mentions,
  mrkdwn-to-Markdown conversion, files/reactions and bot messages. Works with
  Enterprise Grid (each workspace is read separately).
- All three authenticate through the existing persistent browser profile (no app
  registration or API tokens): short-lived Outlook/Slack tokens are read into
  memory only, with silent SSO re-login (`signin_url` / `sso_button_text` for
  Slack) and `workctx auth login-web --source <name>` for the rare human step.
- `workctx doctor` checks for the new sources; generated CONTEXT/AGENTS/CLAUDE/
  PROJECT_BRIEF/CHATGPT docs and the index describe them.
- Rolling-window reconciliation: sources can declare `retention_cutoff()` /
  `reconcile_supported()` so items that merely aged out of a window are kept;
  Teams transcripts honour `since_days` the same way.

### Security
- Tokens are only sent to the configured https API host (no redirects; Slack
  hosts must be `*.slack.com`); never logged (`repr` hidden); every call is a
  read (`GET` / read-only Slack methods).

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
