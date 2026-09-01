# Technical Specification: Work Context Mirror

## Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.13 |
| Package manager | uv |
| Project config | pyproject.toml + uv.lock |
| HTTP client | httpx |
| Validation | Pydantic v2 |
| Configuration | YAML (PyYAML) |
| Secrets | Python keyring → macOS Keychain |
| State database | SQLite |
| Search | SQLite FTS5 |
| Testing | pytest |
| Doc conversion | MarkItDown, PyMuPDF4LLM, Docling (optional) |
| Browser automation | Playwright (optional fallback only) |
| Scheduling | macOS launchd |
| Notifications | Telegram Bot API, macOS Notification Center |

## Package Layout

```
src/workctx/
├── __init__.py
├── cli.py          # Click-based CLI entry point
├── config.py       # YAML loading + Pydantic models
├── state.py        # SQLite state database + migrations
├── sync.py         # Orchestration: discover → fetch → normalise → write
├── models.py       # Shared Pydantic domain models
├── notifications.py # Telegram + macOS notification dispatch
├── indexing.py     # FTS5 indexing + search
├── scheduler.py    # launchd plist generation + management
├── corpus.py       # Output file writing, manifest, LLM instruction files
├── locking.py      # File-based execution lock with stale detection
├── normalise/
│   ├── __init__.py
│   ├── common.py   # Shared normalisation utilities, front matter, splitting
│   ├── office.py   # MarkItDown: DOCX, PPTX, XLSX, CSV, HTML
│   ├── pdf.py      # PyMuPDF4LLM primary, Docling fallback
│   ├── html.py     # HTML → Markdown
│   └── atlassian.py # Confluence storage → MD, ADF → MD
└── sources/
    ├── __init__.py
    ├── base.py      # Abstract source protocol
    ├── confluence.py # Confluence Cloud/DC sync
    ├── jira.py      # Jira Cloud/DC sync
    └── sharepoint.py # OneDrive local, Graph, rclone, Playwright modes
```

## Data Flow

```
Source APIs / Local FS
        │
        ▼
   Source adapter (discover changed objects)
        │
        ▼
   Fetch content (API response / local file read)
        │
        ▼
   Normalise (convert to Markdown + YAML front matter)
        │
        ▼
   Write to temp file → validate → atomic replace
        │
        ▼
   Update SQLite state (version, hash, timestamps)
        │
        ▼
   Update FTS5 index
        │
        ▼
   Update manifest.jsonl
        │
        ▼
   Advance source checkpoint (only after success)
```

## State Database Schema

```sql
-- Schema version tracking
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Per-source sync checkpoints
CREATE TABLE sync_checkpoints (
    source_name TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    last_checkpoint TEXT,         -- ISO timestamp or delta token
    last_success TEXT,
    last_reconciliation TEXT,
    metadata TEXT                 -- JSON for source-specific data
);

-- Individual source objects
CREATE TABLE source_objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,      -- stable ID from source system
    source_key TEXT,              -- human-readable key (e.g. ALPHA-231)
    title TEXT,
    source_url TEXT,
    source_version TEXT,
    source_updated_at TEXT,
    content_sha256 TEXT,
    output_path TEXT,             -- relative path in corpus
    file_size INTEGER,
    file_mtime REAL,
    last_processed_at TEXT,
    last_error TEXT,
    retry_count INTEGER DEFAULT 0,
    UNIQUE(source_name, source_id)
);

-- FTS5 virtual table
CREATE VIRTUAL TABLE IF NOT EXISTS fts_index USING fts5(
    title,
    body,
    source_type,
    source_name,
    source_id,
    source_key,
    source_url,
    output_path,
    updated_at,
    content='source_objects',
    content_rowid='id'
);
```

## Configuration Schema (Pydantic)

See config.py for full Pydantic v2 models. Key structure:

- ProjectConfig (root)
  - version: int
  - project: ProjectInfo (id, name, output_root)
  - schedule: ScheduleConfig (hour, minute)
  - sync: SyncConfig (overlap_minutes, reconciliation_days, max_concurrency, large_document_chars)
  - sources: SourcesConfig
    - confluence: list[ConfluenceSource]
    - jira: list[JiraSource]
    - sharepoint: list[SharePointSource]
  - notifications: NotificationsConfig
    - telegram: TelegramConfig
    - macos: MacOSNotificationConfig

## Authentication

| Source | Method | Storage |
|--------|--------|---------|
| Confluence Cloud | API token (email + token) | macOS Keychain via secret_ref |
| Confluence DC | PAT or basic auth | macOS Keychain via secret_ref |
| Jira Cloud | API token (email + token) | macOS Keychain via secret_ref |
| Jira DC | PAT or basic auth | macOS Keychain via secret_ref |
| SharePoint (local) | None (OneDrive handles auth) | N/A |
| SharePoint (Graph) | Device code flow | macOS Keychain |
| Telegram | Bot token | macOS Keychain via secret_ref |

## Sync Transaction Safety

1. Load checkpoint for source
2. Discover changed objects since checkpoint - overlap
3. For each changed object:
   a. Fetch content
   b. Normalise to temp file
   c. Validate conversion (non-empty, valid front matter)
   d. Atomic replace (os.replace) of corpus file
   e. Update FTS index
   f. Update state DB
4. Handle deletions (reconciliation cycle)
5. Persist new checkpoint only after all changes processed
6. Generate manifest, health, INDEX

## Error Handling

- HTTP 429: respect Retry-After, bounded exponential backoff with jitter
- HTTP 5xx: retry with backoff, max 3 attempts per object
- Connection errors: retry with backoff
- Conversion failure: preserve previous output, log, mark degraded
- Source failure: don't advance checkpoint, preserve corpus, alert
- Lock contention: exit cleanly, log

## Concurrency

- asyncio with semaphore (default max_concurrency=4)
- httpx.AsyncClient for API calls
- Bounded concurrent document processing
