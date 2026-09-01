# Work Context Mirror

Lightweight macOS tool that creates and continuously maintains a local,
LLM-friendly mirror of work knowledge from Confluence, Jira, and
SharePoint/OneDrive.

Configure a project once, schedule a sync, and thereafter have an
automatically refreshed body of project knowledge that ChatGPT, Codex,
Claude Code, or similar tools can interrogate directly from the filesystem.

## How It Works

```
Confluence (Cloud / Data Center)
Jira       (Cloud / Data Center)
SharePoint (via local OneDrive sync)
            │
            ▼
   Work Context Mirror
   (incremental sync)
            │
            ▼
   Clean Markdown corpus
   with YAML front matter
   (in OneDrive / local folder)
            │
     ┌──────┼──────┐
     ▼      ▼      ▼
  ChatGPT  Codex  Claude
```

- **Confluence** pages → individual Markdown files with metadata
- **Jira** issues → Markdown files including comments, links, custom fields
- **SharePoint** documents (Word, PDF, Excel) → converted Markdown
- Only changed content is reprocessed on incremental syncs
- Previous good versions are preserved if a conversion fails

## Quick Start

```bash
# Clone and install
git clone https://github.com/visser23/work-context-builder.git
cd work-context-builder
uv sync

# Interactive setup (generates a YAML config)
uv run workctx init

# Store secrets in macOS Keychain
uv run workctx auth set my-confluence-pat
uv run workctx auth set my-jira-pat

# Validate everything
uv run workctx doctor --config workctx.yaml

# First sync
uv run workctx sync --config workctx.yaml --full

# Schedule daily
uv run workctx install-schedule --config workctx.yaml
```

## Atlassian Authentication

Work Context Mirror supports both **Atlassian Cloud** and **Data Center**
instances. The authentication method depends on your deployment type.

### Cloud (*.atlassian.net)

Uses API tokens with Basic Auth.

1. Go to <https://id.atlassian.com/manage-profile/security/api-tokens>
2. Click **Create API token** → copy it
3. Store it:

```bash
uv run workctx auth set my-cloud-token
```

Config:

```yaml
sources:
  confluence:
    - name: my-wiki
      base_url: "https://myorg.atlassian.net"
      deployment: cloud          # or "auto" (default)
      spaces: [ENG, OPS]
      auth:
        mode: api_token
        username: "you@company.com"
        secret_ref: my-cloud-token
```

### Data Center (self-hosted)

Uses Personal Access Tokens (PATs) with Bearer auth. No username required.

1. In your DC instance, go to **Profile → Personal Access Tokens**
2. Create a token with read permissions → copy it
3. Store it:

```bash
uv run workctx auth set my-dc-confluence-pat
uv run workctx auth set my-dc-jira-pat
```

Config:

```yaml
sources:
  confluence:
    - name: my-dc-wiki
      base_url: "https://confluence.myorg.com"
      deployment: datacenter     # or "auto" — auto-detects DC
      spaces: [PROJ, TEAM]
      auth:
        mode: pat
        secret_ref: my-dc-confluence-pat

  jira:
    - name: my-dc-jira
      base_url: "https://jira.myorg.com"
      deployment: datacenter
      projects: [PROJ, OPS]
      auth:
        mode: pat
        secret_ref: my-dc-jira-pat
      include_comments: true
```

### Auto-Detection

When `deployment` is set to `"auto"` (the default), Work Context Mirror
probes the instance to determine Cloud vs. Data Center:

- URLs containing `.atlassian.net` → Cloud
- Successful Bearer-token auth against `/rest/api/2` → Data Center
- Fallback to Cloud-style Basic Auth if both fail

For reliability, explicitly setting `deployment: datacenter` or
`deployment: cloud` is recommended.

## SharePoint / OneDrive Setup

Work Context Mirror reads SharePoint through the **official OneDrive
macOS client**. No Microsoft admin or Entra application registration
required.

### Steps

1. Sign into OneDrive on your Mac with your work account
2. In SharePoint, navigate to the document library
3. Click **Sync** or **Add shortcut to OneDrive**
4. Note the resulting local path (usually under `~/Library/CloudStorage/OneDrive-YourOrg/`)
5. Use that path as `local_path` in your config

### Finding the Local Path

```bash
ls ~/Library/CloudStorage/
# Look for your organisation's OneDrive folder
```

### Config

```yaml
sources:
  sharepoint:
    - name: team-docs
      mode: onedrive_local
      local_path: "~/Library/CloudStorage/OneDrive-YourOrg/SharedDocs"
      include: ["**/*"]
      exclude: ["**/~$*", "**/.DS_Store", "**/*.tmp"]
```

### Limitations

The `onedrive_local` mode only works when the SharePoint library is
synced to the local machine via OneDrive. If the library is not synced,
browser-based access is planned for a future release.

## Telegram Notifications

Optional alerts for sync completions, failures, and recoveries.

### Setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
2. Copy the bot token
3. Start a chat with your new bot (send any message)
4. Get your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`

### Store Credentials

```bash
uv run workctx auth set telegram-bot-token
# Paste the bot token

uv run workctx auth set telegram-chat-id
# Paste the chat ID
```

### Config

```yaml
notifications:
  telegram:
    enabled: true
    bot_token_ref: telegram-bot-token
    chat_id_ref: telegram-chat-id
  macos:
    enabled: true
```

## Configuration Reference

Full example: [`example-config.yaml`](example-config.yaml)

```yaml
version: 1

project:
  id: my-project                    # Unique ID, used for state directory
  name: "My Project"                # Human-readable name
  output_root: "~/path/to/output"   # Where Markdown files are written

schedule:
  hour: 6                           # Daily sync hour (24h)
  minute: 0

sync:
  overlap_minutes: 15               # Re-check window to catch late changes
  reconciliation_days: 7            # Days between full deletion sweeps
  max_concurrency: 4
  large_document_chars: 300000      # Split threshold for huge docs

sources:
  confluence: [...]
  jira: [...]
  sharepoint: [...]

notifications:
  telegram: { enabled: true, bot_token_ref: "...", chat_id_ref: "..." }
  macos: { enabled: true }
```

## CLI Reference

| Command | Description |
|---|---|
| `workctx init` | Interactive config generator |
| `workctx doctor` | Validate config and environment |
| `workctx sync` | Run incremental sync |
| `workctx sync --dry-run` | Preview without modifying |
| `workctx sync --full` | Force full resync |
| `workctx status` | Show project and sync status |
| `workctx search "query"` | Full-text search the corpus |
| `workctx reconcile` | Force deletion reconciliation |
| `workctx reindex` | Rebuild FTS5 search index |
| `workctx auth set <ref>` | Store a secret in Keychain |
| `workctx auth remove <ref>` | Remove a secret |
| `workctx install-schedule` | Install launchd daily schedule |
| `workctx remove-schedule` | Remove the schedule |
| `workctx schedule-status` | Check scheduler status |

All commands accept `--config <path>` to specify the YAML config file.

## Output Structure

```
OutputRoot/
├── README.md             # Auto-generated corpus overview
├── CONTEXT.md            # Corpus explanation for humans & LLMs
├── AGENTS.md             # Instructions for Codex-style agents
├── CLAUDE.md             # Instructions for Claude Code
├── _meta/
│   ├── INDEX.md          # Source overview with counts
│   ├── health.json       # Sync health status
│   └── manifest.jsonl    # Per-document metadata
├── confluence/
│   └── <source-name>/
│       └── <SPACE>/
│           └── <id>-<page-title>.md
├── jira/
│   └── <source-name>/
│       └── <PROJECT>/
│           └── <PROJ-123>.md
└── sharepoint/
    └── <source-name>/
        └── <path>/
            └── <document>.md
```

Each Markdown file includes YAML front matter:

```yaml
---
source_type: confluence
source_name: my-wiki
source_id: "12345"
title: "Architecture Overview"
source_url: "https://confluence.example.com/pages/viewpage.action?pageId=12345"
space: ENG
source_version: "42"
updated_at: "2026-08-15T10:30:00+00:00"
synced_at: "2026-09-01T06:00:12+00:00"
---
```

## State & Logs

Runtime state is stored locally (not in the output directory):

```
~/Library/Application Support/WorkContextMirror/<project-id>/
├── state.sqlite       # Sync state, checkpoints, FTS5 index
├── logs/              # Rotating log files
├── tmp/               # Temp files during conversion
└── run.lock           # Execution lock
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Config file not found | Use `--config /path/to/config.yaml` |
| No API token found | `workctx auth set <secret_ref>` |
| Another sync is running | Delete stale `run.lock` if process crashed |
| SharePoint path missing | Ensure OneDrive sync is active: `ls ~/Library/CloudStorage/` |
| Confluence 401 | Check PAT hasn't expired; regenerate in DC profile settings |
| Jira timeout on DC | Jira DC can be slow on large searches; first sync takes longest |
| PDF conversion issues | `uv pip install pymupdf4llm` |

Run comprehensive diagnostics:

```bash
uv run workctx doctor --config workctx.yaml --verbose
```

## Security Notes

- Secrets are stored in **macOS Keychain only** — never in config files or logs
- All document processing happens **locally** — nothing sent to external services
- Source systems are accessed **read-only**
- A `SecretFilter` strips any token patterns from log output

**Important:** Synchronising organisational information into a locally
controlled directory may be restricted by your employer. Ensure the
chosen storage location complies with organisational
information-governance requirements.

## Development

```bash
# Install dev dependencies
uv sync --extra dev

# Run tests
uv run pytest

# Run with coverage
uv run pytest --cov=workctx

# Lint
uv run ruff check src/ tests/

# Type-check
uv run mypy src/
```

## License

MIT
