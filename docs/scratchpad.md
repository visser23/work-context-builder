# Scratchpad: Work Context Mirror

## Current Status
- Project initialised: 2026-09-01
- Phase 1-6 complete
- 85 tests passing, 0 lint errors
- CLI operational: `uv run workctx --version` confirms 0.1.0
- Doctor passes: 18/18 checks against NHS DC instances
- First full sync running (Jira DC + Confluence DC)

## Live Environment
- Confluence DC: `nhsd-confluence.digital.nhs.uk` → spaces HOM, DTX (~1291 pages)
- Jira DC: `nhsd-jira.digital.nhs.uk` → projects HOTE, DTX (~1800 issues)
- SharePoint: `nhs.sharepoint.com/sites/X26_Digital_Prevention_Service` → NOT locally synced
- Telegram bot: `mv_nhs_sync_bot` → chat ID 8337793696, notifications working
- Git: committed to `main`, remote push pending (mia-oc creds don't have access to visser23 repo)

## Build Summary
- Python 3.13.15 via uv
- 61 dependencies resolved and installed
- All core modules implemented
- Phase 7 (optional fallbacks) deferred by design

## Authentication
- Atlassian Cloud: API tokens via Basic Auth (username + token)
- Atlassian Data Center: Personal Access Tokens via Bearer auth
- Auto-detection: probes `/rest/api/2/myself` with Bearer, falls back to Cloud
- NHS instances confirmed as Data Center (not Cloud)
- DC uses API v2 (not v3), no `/wiki` prefix for Confluence

## Key Findings
- Jira project key for HomeTest is `HOTE` (not `HOM` — that's the Confluence space key)
- NHS Confluence API base is `/rest/api` (no `/wiki` prefix for DC)
- NHS SharePoint not synced via OneDrive locally — `OneDrive-ServitaGroupLtd` contains
  personal Servita files only. Browser-based SP access needed for future.
- Confluence DC returns empty `body.storage.value` for ~12% of pages (restricted/draft)
- Jira DC initial full sync is slow (~30+ min for 1800 issues with comments)

## Lessons
- `fnmatch.fnmatch` treats `**/*` literally. Must strip `**/` prefix and
  match against basename separately.
- ruff `--fix` handles import sorting and simple patterns, but E501 requires
  manual line breaking for readability.
- Doctor module uses imports purely for availability checking (try/import/except)
  — these need `# noqa: F401` to avoid false positives.
- Atlassian Cloud tokens (ATATT3x...) do NOT work for Data Center instances.
  DC requires Personal Access Tokens with Bearer auth.
- Confluence DC API does not use `/wiki/rest/api` prefix — it's just `/rest/api`.
  Auto-detection probes both paths.
- Jira DC API v2 uses a different field format than v3 (description is plain text
  or wiki markup, not ADF). The ADF converter handles both gracefully.
- 30s timeout is insufficient for Jira DC search on large projects — increased to 120s.
- Empty Confluence pages should produce metadata stubs, not errors.

## Open Questions
- SharePoint browser-based access: Playwright with NHS Chromium profile, or
  semantic-browser CLI? Both are available on the machine.
- Phase 7 features depend on user needs — implement on demand
