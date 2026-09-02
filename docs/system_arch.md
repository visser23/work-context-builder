# System Architecture: Work Context Mirror

## High-Level Data Flow

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────────────┐
│   Confluence     │   │      Jira        │   │  SharePoint / OneDrive   │
│   (REST/CQL)     │   │   (REST/JQL)     │   │  (local filesystem)      │
└────────┬─────────┘   └────────┬─────────┘   └────────────┬─────────────┘
         │                      │                           │
         └──────────────────────┼───────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │   Source Adapters     │
                    │   (discover deltas)   │
                    └───────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │   Sync Orchestrator   │
                    │   (fetch → normalise  │
                    │    → write → index)   │
                    └───────────┬───────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                  │
    ┌─────────▼──────┐  ┌──────▼───────┐  ┌──────▼───────┐
    │  Normalisers   │  │  SQLite DB   │  │   Corpus     │
    │  (MD, Office,  │  │  (state,     │  │   Output     │
    │   PDF, ADF)    │  │   FTS5)      │  │   (Markdown) │
    └────────────────┘  └──────────────┘  └──────┬───────┘
                                                  │
                                           OneDrive Folder
                                                  │
                        ┌─────────────────────────┼──────────────┐
                        │                         │              │
                   ChatGPT Work              Codex         Claude Code
```

## Component Map

```
src/workctx/
│
├── cli.py ──────────────── User entry point (Click commands)
│                            ├── init, doctor, sync, status, search
│                            ├── auth set/remove
│                            ├── reconcile, reindex
│                            └── install-schedule, remove-schedule, schedule-status
│
├── config.py ───────────── YAML → Pydantic models
│                            └── Validates config, resolves paths
│
├── models.py ───────────── Shared domain models
│                            ├── SourceObject, SyncResult, RunStatus
│                            └── FrontMatter, ManifestEntry
│
├── state.py ────────────── SQLite state management
│                            ├── Schema migrations
│                            ├── CRUD for source_objects
│                            ├── Checkpoint management
│                            └── FTS5 operations (delegated to indexing.py)
│
├── sync.py ─────────────── Orchestration engine
│                            ├── For each source: discover → fetch → normalise → write
│                            ├── Atomic file replacement
│                            ├── Checkpoint advancement
│                            └── Run status aggregation
│
├── corpus.py ───────────── Output management
│                            ├── Atomic file writing
│                            ├── Manifest generation
│                            ├── LLM instruction files (CONTEXT, AGENTS, CLAUDE)
│                            ├── INDEX.md generation
│                            └── health.json generation
│
├── indexing.py ──────────── FTS5 search
│                            ├── Index building / updating
│                            └── Search query execution
│
├── locking.py ──────────── Execution lock
│                            ├── PID-based lock file
│                            └── Stale lock detection
│
├── scheduler.py ─────────── Service management
│                            ├── macOS launchd (KeepAlive daemon)
│                            ├── Linux systemd (user service)
│                            ├── Windows Task Scheduler
│                            └── Legacy fixed-time schedule
│
├── daemon.py ────────────── Background daemon
│                            ├── Daily sync trigger (24h interval)
│                            ├── Telegram command polling
│                            └── On-demand sync via /sync, /syncfull
│
├── progress.py ──────────── Rich progress display
│                            ├── Per-source progress bars with ETA
│                            └── Summary table
│
├── notifications.py ─────── Alert dispatch
│                            ├── Telegram Bot API
│                            ├── macOS Notification Center
│                            └── Alert suppression
│
├── normalise/
│   ├── common.py ────────── Front matter, document splitting, utilities
│   ├── office.py ────────── MarkItDown: DOCX, PPTX, XLSX, CSV
│   ├── pdf.py ───────────── PyMuPDF4LLM (primary), Docling (fallback)
│   ├── html.py ──────────── HTML → Markdown
│   └── atlassian.py ─────── Confluence storage XML → MD, Jira ADF → MD
│
└── sources/
    ├── base.py ──────────── Source protocol (abstract interface)
    ├── confluence.py ────── Confluence Cloud/DC adapter
    ├── jira.py ──────────── Jira Cloud/DC adapter
    ├── sharepoint.py ────── SharePoint local (OneDrive) mode
    ├── sharepoint_web.py ── SharePoint browser mode (REST API)
    └── local_folder.py ──── Local filesystem directory scanner
```

## Storage Layout

```
Runtime State (platform-dependent, or overridden via state_dir):
macOS:   ~/Library/Application Support/WorkContextMirror/<project-id>/
Linux:   ~/.local/share/WorkContextMirror/<project-id>/
Windows: ~/AppData/Local/WorkContextMirror/<project-id>/
├── state.sqlite          # Source objects, checkpoints, FTS5
├── logs/                 # Rotating log files
│   └── 20260901-053001.log
├── tmp/                  # Temp files during conversion
├── auth/                 # Browser state (Playwright only)
└── run.lock              # Execution lock

LLM Corpus (in OneDrive):
<output_root>/
├── README.md
├── CONTEXT.md
├── AGENTS.md
├── CLAUDE.md
├── _meta/
│   ├── INDEX.md
│   ├── health.json
│   └── manifest.jsonl
├── confluence/<source-name>/<space>/<id>-<slug>.md
├── jira/<source-name>/<project>/<ISSUE-KEY>.md
└── sharepoint/<source-name>/<relative-path>.md
```

## Sync Flow (per source)

```
1. Acquire execution lock
2. Load config + state DB
3. For each configured source:
   a. Load checkpoint
   b. Discover changes (API query or filesystem scan)
   c. For each changed object:
      i.   Fetch content
      ii.  Normalise to Markdown (temp file)
      iii. Validate output
      iv.  Atomic replace in corpus
      v.   Update FTS5 index
      vi.  Update state DB
   d. Handle deletions (if reconciliation due)
   e. Advance checkpoint
4. Generate manifest, health, INDEX, LLM files
5. Release lock
6. Send notifications if needed
```
