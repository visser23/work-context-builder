# Scratchpad: Work Context Mirror

## Current Status
- All core phases complete + LLM integration polish + external review hardening
- 234 tests passing, 0 lint errors in touched files (pre-existing RUF005/E501 in scheduler.py/doctor.py)
- Background daemon with Telegram commands (launchd KeepAlive on macOS)
- Cross-platform service management (launchd, systemd, Task Scheduler)
- First-run bootstrap scripts for macOS/Linux and Windows
- SharePoint incremental delta via GetChanges API (ChangeToken persisted in checkpoint metadata)
- Cookie keepalive: daemon tests SharePoint cookies every 4h; on rejection it runs the headless profile refresh (waits up to 45s for silent SSO) and only alerts via Telegram if that fails
- Run-level failures (e.g. manifest write error) now surface as FAILED in `aggregate_status()` instead of "healthy"
- `_meta/*` writes are atomic + retry on EDEADLK/ETIMEDOUT/EAGAIN/EBUSY (OneDrive File Provider)
- Dedup: uses (source_name, source_id) UNIQUE constraint + source_version + content_sha256
- Version-only changes (same content hash) now update source_version in DB without file rewrite
- LLM-optimised output: PROJECT_BRIEF.md, CHATGPT_INSTRUCTIONS.md, CLAUDE.md, AGENTS.md
- Jira summary: SUMMARY.csv + SUMMARY.md per source for Gantt/status views
- **Self-contained local install**: daemon venv is non-editable (no OneDrive dependency for imports)
- **Path length clamping**: corpus output paths limited to 380 chars to prevent OneDrive crashes
- **Login wrapper**: `workctx-relogin` script works without OneDrive being available

## Architecture Notes
- SharePoint list name may differ per tenant: "Documents" vs "Shared Documents" — `doc_library` in config
- GetChanges API: `FetchLimit` parameter causes `Edm.Int64` errors on some SP tenants — removed
- ChangeToken stored in `sync_checkpoints.metadata` (JSON), not `last_checkpoint` (ISO timestamp)
- Cookie validation: cache-first strategy — lightweight HTTP GET before Playwright launch
- Playwright uses `wait_until="domcontentloaded"` (not `networkidle`) to avoid SP background request timeouts
- Checkpoint safety: never advance past failed objects (tracks earliest failure timestamp)
- Resource cleanup: DB/index/adapters closed in `finally` blocks even on exception
- SharePoint modes restricted to `onedrive_local` and `browser` (graph/rclone removed as unimplemented)
- Auth modes restricted to `api_token`, `pat`, `basic`, `browser` (device_code removed as unimplemented)

## Lessons
- External review: prioritise by actual threat model (local CLI ≠ SaaS), not OWASP severity theatre.
- SP deletion detection: never use substring matching for identity lookups — persist proper IDs.
- Daemon freshness: use stalest (min) source, not freshest (max), to trigger daily sync.
- Multipart cleanup: always remove ALL old parts before writing new ones to avoid corpus zombies.
- Default-deny for file extensions: unknown types should be rejected, not optimistically converted.
- Trust boundary disclaimers in LLM instruction files cost nothing and are responsible framing.
- Schema migrations: SQLite ALTER TABLE ADD COLUMN + COALESCE in upsert preserves existing data cleanly.
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
- ChatGPT Projects: 5-40 file limit → single PROJECT_BRIEF.md critical for quick context
- Claude Projects: RAG handles large corpora, CLAUDE.md should be concise (<200 lines)
- Dead code removal: html.py converter was unused (HTML goes through MarkItDown), ChangeAction.RENAME never referenced
- OneDrive crashes when corpus output files exceed 400-char path limit — clamped to 380.
- OneDrive `os error 60` timeouts occur when the app isn't running or is crashed — file reads silently hang.
- `uv run --project` creates editable installs (.pth pointing to source dir). For cloud storage projects, non-editable `uv pip install` is required.
- `os.path.splitext` treats any dot as an extension boundary. File names like "Item 2.3 - Title" need max-extension-length guard.
- OneDrive's 400-char limit applies to its internal DISPLAY path, not the macOS filesystem path. The display path is ~17 chars shorter.
- `--clear` flag needed on `uv venv` when recreating an existing venv.
- Playwright browsers are installed per-system, not per-venv. Must run `playwright install chromium` after building a new venv.
- **SharePoint "session expired but I'm still logged in" (Oct 2026)** — ROOT CAUSE: browser-profile `rtFa`/`FedAuth` are persistent (~120h) and Entra `ESTSAUTHPERSISTENT` lasts ~90d, but SharePoint invalidates the session server-side after hours. A headless navigation then bounces SP -> login.microsoftonline.com -> back, which takes 2-4s of silent SSO. `keepalive_and_extract` read cookies/URL right at `domcontentloaded` (mid-redirect: cookies cleared, URL = login) and gave up with "redirected to login". Fix: poll up to 45s for cookies that pass an HTTP check; never trust the first cookies seen (stale ones are present immediately). Same flaw in `interactive_login` (stored unvalidated stale profile cookies) — now validated. The daemon's 4h check also alerted without ever trying a refresh — now it refreshes first.
- Dogfooding method that found it: (1) read daemon log timeline (fail 06:22->08:24 then "refreshed" 08:54 = profile was never logged out), (2) read profile cookie DB expiries (names + expiry only, never values), (3) reproduce on a COPY of the profile by `add_cookies` with garbage `FedAuth`/`rtFa` (server-side-expiry simulation), (4) re-run the real function. Always use a throwaway keychain ref/profile for experiments.
- A failing `result.status` was masked because the daemon used `aggregate_status()` (per-source only). Check ALL status sources when a log says "failed" and "healthy" back to back.
- OneDrive File Provider raises transient EDEADLK (errno 11) / ETIMEDOUT (60) on open-for-write of materialised files; use temp-file + `os.replace` with retry.
- TODO (not done): `SharePointWebSource.get_current_ids` swallows per-folder non-200/exception and returns a PARTIAL id set; reconcile then deletes everything missing. Verified Oct 7 deletions were genuine (88/88 gone on server), but a throttled enumeration could mass-delete. Make it raise on any non-200/404 so reconciliation is skipped.
