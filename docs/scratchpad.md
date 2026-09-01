# Scratchpad: Work Context Mirror

## Current Status
- Project initialised: 2026-09-01
- Phase 1-6 complete
- 85 tests passing, 0 lint errors
- CLI operational: `workctx --version` confirms 0.1.0

## Build Summary
- Python 3.13.15 via uv
- 61 dependencies resolved and installed
- All core modules implemented
- Phase 7 (optional fallbacks) deferred by design

## Notes
- doc-llm-processor in parent folder uses Unstructured (heavy, Torch-based) —
  Work Context Mirror uses MarkItDown + PyMuPDF4LLM instead (much lighter)
- `fnmatch` doesn't support `**` globs — fixed with `_strip_glob_prefix()` helper
  in SharePoint adapter for recursive pattern matching
- MarkItDown 0.1.7 is the current stable version with `.text_content` API
- PyMuPDF4LLM 1.28.2 provides `to_markdown()` function directly

## Lessons
- `fnmatch.fnmatch` treats `**/*` literally (doesn't match `README.md`).
  Must strip `**/` prefix and match against basename separately.
- ruff `--fix` handles import sorting and simple patterns, but E501 requires
  manual line breaking for readability.
- Doctor module uses imports purely for availability checking (try/import/except)
  — these need `# noqa: F401` to avoid false positives.

## Open Questions
- Phase 7 features depend on user needs — implement on demand
