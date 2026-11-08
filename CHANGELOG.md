# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [1.6.0]

### Added
- **`workctx` works in any shell.** `install-service` now writes a `workctx` launcher (next to
  `workctx-relogin`) into `~/.local/bin`, for cloud-synced projects (self-contained venv) and
  for others that have no `workctx` on PATH (`uv run --project ...`). It never overwrites a
  file it did not generate. Previously only `workctx-relogin` existed, so `workctx sources`
  was "command not found" outside the repo and the README's examples could not be followed.
- **`WORKCTX_CONFIG`** environment variable: the config used when `--config` is not given
  (explicit `--config` wins; then `./workctx.yaml`). The launcher sets it, so every command
  works from any directory. A missing file is reported with where the path came from.
- **`workctx auth relogin [NAME] [--force]`**: one command for every browser login
  (SharePoint, Teams transcripts, email, calendar, Slack). Each is checked silently and a
  window opens only where a login has lapsed. `workctx-relogin` now runs it (so
  `workctx-relogin --source my-slack` and `workctx-relogin my-slack` both work). An
  unknown name lists the sources that can be signed in to.

### Changed
- `workctx --help` ends with an everyday-commands guide and explains config discovery; `auth`
  has a real summary; every `--config` option says where its default comes from.
- README: new "How to run workctx" section, corrected CLI reference and troubleshooting.

## [1.5.1]

### Fixed
- **Slack sign-in window never closed / never detected the login.** The window started on the
  workspace sign-in page and, after SSO, stayed on `*.enterprise.slack.com`, but Slack only
  stores its tokens on `app.slack.com`, so nothing was ever found. The window now starts on the
  Slack client: a live session is detected immediately; a stale one is sent to the sign-in page
  once; and when Slack issues a new login cookie the window is carried back to the client so the
  tokens appear. It never navigates while you are on the identity provider's pages.

### Changed
- **`workctx auth login-web` only opens windows for sessions that have lapsed.** Each selected
  source is first checked silently (headless, ~20s at most); a still-valid session prints
  "already signed in" and no window opens. `--force` opens the window regardless.

## [1.5.0]

### Added
- **`workctx sources`** lists every configured source (kind, name, login/browser profile,
  URLs and windows) plus the config file, output and state locations. No secrets are shown.

### Fixed
- **"Slack login failed" every sync while logged in.** Slack keeps stale `xoxc-` tokens in
  the browser profile until the page itself notices they are dead, bounces to
  `/workspace-signin` and wipes them (about 4 seconds). We read the tokens at ~2 seconds,
  called that a session, and a refresh re-read the same ones, so every call returned
  `invalid_auth`. The session is now checked with `auth.test`; a refused one falls through to
  the SSO sign-in (`signin_url`), which renews it silently. Inconclusive checks (rate
  limiting, network) never discard a session. The lapsed-client wait is capped at 25s before
  the sign-in page is tried.
- **`workctx-relogin --source <slack/mail/calendar source>`** said "SharePoint source not
  found". `auth login-sharepoint` now hands web sources to `auth login-web`, and an unknown
  name points at `workctx sources`.
- Refreshing Slack no longer wipes Outlook's record of revoked tokens (`clear_slack_cache`).

## [1.4.9]

### Fixed
- **Email and calendar failed with "Outlook rejected the browser session token" while you were
  logged in.** Outlook's continuous access evaluation can revoke a token server-side (HTTP 401
  `insufficient_claims`) while its cached expiry is still hours away. We took the first token
  from the browser's cache without checking it, and the "forced refresh" re-read that same
  revoked token, so every run gave up. Tokens are now probed against Outlook; refused ones are
  remembered and skipped while the browser (which renews them itself within seconds) produces a
  working one. A throttled or unreachable probe is treated as inconclusive and never costs a
  working token. The error now says what Outlook actually answered.
- **`workctx auth login-web` demanded `--source`.** It is now optional and signs in every
  mail/calendar/Slack source (sources sharing a profile and URL open the browser once). The
  interactive sign-in also validates the token instead of declaring success on a revoked one.
- Hints no longer tell you to run `uv run workctx auth login-web --source <name>`.

## [1.4.8]

### Fixed
- **A big SharePoint backlog starved every other source.** All sources shared one first-in,
  first-out pool of 4 workers, so ~4,000 slow PDF/Office conversions (a single PDF can take
  2+ minutes) queued ahead of ~26,000 quick Jira/mail/Slack/calendar/Teams writes, which then
  sat idle for hours on a machine with nothing else to do for them. Sources that convert heavy
  documents (`heavy_conversion = True`: SharePoint) now run in their own lane (half the
  workers, at most 2); the text sources keep the shared pool and finish in minutes. If every
  configured source is heavy they still share the whole pool.

## [1.4.7]

### Fixed
- **"SharePoint kept rejecting the session — run workctx-relogin" while already logged in.**
  SharePoint rate-limits by *redirecting* to `/_layouts/15/Throttle.htm`, which looks just like
  the bounce to the login page. Every redirect was read as an expired login: downloads burned
  the 3-per-run refresh budget in seconds (each "refresh" rebuilt the client from the same
  cookies, no browser involved), after which every remaining file failed and the alert told
  you to log in again. Now:
  - Throttling is recognised (`is_throttle_redirect`) in the session probe, normal requests,
    downloads and Teams transcripts: all SharePoint requests pause (15 s, 30 s, 60 s ... capped
    at 2 min) instead of hammering on, and the login is left alone.
  - A refused download is checked against the session probe first; the shared refresh budget is
    only for a genuinely rejected session and now resets after the next accepted request.
  - A login done elsewhere (`workctx-relogin`, the keep-alive) is picked up by the running
    daemon without a browser. If downloads stay refused while the probe passes, one rationed
    forced fresh login is requested from the browser profile (`keepalive_and_extract(fresh=True)`).
  - A single refused file now fails that file (retried next run), not the whole source.
  - Teams transcripts retries a single 401/redirect once before reporting an expired login.
  - Logs now say what SharePoint answered (status and redirect target).

## [1.4.6]

### Fixed
- **The corpus held ~3,300 files while the index listed ~30,000.** The state DB lives outside
  the cloud-synced output folder, so when OneDrive reset that folder (21 Oct) the files
  vanished but every row still said "synced". Incremental discovery only looks at what changed
  at the source (and `--full` skips rows whose version/hash still match), so nothing was ever
  re-created. Every sync now checks the disk first (`workctx.drift`): documents whose files
  are missing are marked for re-fetch (version, content hash, retry count cleared) and
  re-downloaded; checkpoint-bound sources (Jira, Confluence, Slack, local folders) are given a
  full listing for that run. A busy cloud folder (EDEADLK/ETIMEDOUT) counts as *unknown*,
  never as missing. The check repeats every run, so a document that fails is retried.
- **Every write cost a full scan of the search index (~5 s at 30k documents).** FTS5 cannot index
  a column, so `DELETE FROM fts_index WHERE output_path = ?` (run on each upsert/remove) read
  every row; a 27k-document repair would have taken days. `SearchIndex` now keeps an indexed
  `fts_paths` map (output path -> FTS rowid): ~0.2 ms per write. Existing databases are
  upgraded automatically on first open (~4 s; historical duplicate rows are collapsed).
- **The daemon now notices missing files itself** (at start-up and every 6 h) and starts a
  repair sync instead of waiting for the next daily sync.
- **Fresh installs failed on Python 3.14**: `onnxruntime` (via `markitdown`/`magika`) has no 3.14
  wheels. `requires-python` is now `>=3.12,<3.14`, so `uv` picks a supported interpreter
  automatically.
- **Clearing OS caches broke every browser refresh.** Playwright downloads Chromium to
  `~/Library/Caches/ms-playwright` (`~/.cache` on Linux), which cache cleaners delete; the
  daemon then failed with "Executable doesn't exist" on every SharePoint / Outlook / Slack
  session refresh. Chromium now lives in a persistent folder
  (`<app data>/WorkContextMirror/ms-playwright`): `workctx.browsers` pins
  `PLAYWRIGHT_BROWSERS_PATH` (an explicit value inside a `Caches`/`.cache` dir is overridden),
  re-installs Chromium automatically if it is missing at launch, `install-service` installs it
  there and passes the variable to launchd/systemd/Task Scheduler and `workctx-relogin`, and
  `doctor` reports whether Chromium is present.

### Added
- `workctx verify`: read-only per-source table of indexed vs present documents (exit 1 if any
  are missing).

## [1.4.5]

### Fixed
- **A truncated Outlook response failed the whole mail source.** A 200 whose JSON body was cut
  off mid-document (`Expecting ',' delimiter: line 1 column 33035`) escaped the client's retry
  logic, which only covered transport errors and 429/503/504. `OutlookClient.get_json` now
  re-fetches (shared back-off, up to `MAX_JSON_RETRIES`) and only then raises a clear
  "malformed JSON" error naming the endpoint and the byte count.
- **Idle runs re-uploaded ~20 MB through OneDrive.** Every run rewrote the manifest
  (`_meta/manifest.jsonl`, ~19 MB), `PROJECT_BRIEF.md`, the Jira `SUMMARY.*` files and the
  instruction files even when nothing had changed, so a sync client uploaded them all again.
  They are now written through `write_text_if_changed`, which leaves an identical file alone.
  Only the exact "generated at" stamp lines are ignored when comparing (`Last updated:`,
  `Auto-generated on`, `N issues as of`); a date anywhere else is real content. `INDEX.md` and
  `health.json` (a few hundred bytes, deliberate liveness signals) are still rewritten.
- **A failed run was retried every 30 minutes, indefinitely.** Each retry re-walked every source
  and regenerated the corpus. The retry delay now doubles per consecutive failure (30 min, 1 h,
  2 h ... capped at 6 h) and resets after a healthy run.

## [1.4.4]

### Fixed
- **Daemon could not touch cloud-only OneDrive/iCloud files or folders (`EDEADLK`, "Resource
  deadlock avoided").** Root cause: a process started by `launchd` has the macOS per-process I/O
  policy `IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES` set to *off*, so any `stat`/`listdir`/`mkdir`/
  `open` on a dataless placeholder fails at once instead of waiting for the File Provider. It
  stayed hidden while everything in the output folder was already downloaded, and surfaced after a
  OneDrive reset made nearly every file and folder a placeholder. The same path worked from a
  shell because user shells have the policy on. `run_sync` now switches it on for the process
  (`workctx.macos_fs.enable_dataless_materialization`, `setiopolicy_np`); no-op off macOS and
  never raises. The retries added in 1.4.2/1.4.3 stay as a safety net for genuine hiccups.

## [1.4.3]

### Fixed
- **Per-item writes did not retry transient File Provider errors.** Only the `_meta` files used
  the retrying writer; documents went through `write_corpus_file`, which failed an item on the
  first `EDEADLK`. Right after OneDrive was reset or re-linked (everything a placeholder, folders
  not yet enumerated) `mkdir` on such a folder raised `EDEADLK` and the item was lost for that run.
  `write_corpus_file` now uses `write_text_atomic`, and the folder creation is inside its retry
  loop.

## [1.4.2]

### Fixed
- **A transient filesystem error in the pre-sync housekeeping aborted the whole run.** On a
  cloud-synced output folder (OneDrive File Provider) `stat` can fail with `EDEADLK` /
  `EAGAIN` while the provider is busy; pruning stale twins of split documents let that error
  escape and the run failed before any source was read. Pruning now skips the unreadable path
  (it is retried next run), and the idempotent housekeeping steps (relayout, twin pruning,
  long-path migration) are best-effort: a filesystem error is logged as a warning, not fatal.

## [1.4.1]

### Fixed
- **Unchanged items were rewritten on every run.** The change check hashed the whole file,
  including `synced_at` (set to "now"), so the hash never matched the stored one and every item
  a source returned was written again. Sources that re-emit a window (calendar re-emits its
  past 365 / next 90 days) therefore rewrote hundreds of identical files per run, which a
  file-sync client such as OneDrive uploads again each time. `synced_at` is now excluded from
  the hash, so only items whose content or front matter actually changed are written.
- An unchanged stub is not rewritten but still counts towards the retry cap, so a file that
  cannot be converted still gives up after `MAX_STUB_RETRIES`.

### Notes
- The first run after upgrading rewrites each item a source returns once, because stored hashes
  from earlier versions included `synced_at`. After that, identical items are left alone.

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
