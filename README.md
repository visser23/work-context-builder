# Work Context Mirror

Cross-platform tool that creates and continuously maintains a local,
LLM-friendly mirror of work knowledge from Confluence, Jira, and
SharePoint/OneDrive.

Configure a project once, and a background daemon keeps an automatically
refreshed body of project knowledge that ChatGPT, Codex, Claude Code,
or similar tools can interrogate directly from the filesystem. Trigger
syncs on-demand from Telegram or let the daemon handle it daily.

**Supports:** macOS, Windows, Linux | Python 3.12+

## How It Works

```
Confluence (Cloud / Data Center)
Jira       (Cloud / Data Center)
SharePoint (OneDrive sync or browser-based)
            |
            v
   Work Context Mirror
   (background daemon)
            |
            v
   Clean Markdown corpus        <--- Telegram: /sync, /status
   with YAML front matter
            |
     +------+------+
     v      v      v
  ChatGPT  Codex  Claude
```

- **Confluence** pages -> individual Markdown files with metadata
- **Jira** issues -> Markdown files including comments, links, custom fields
- **SharePoint** documents (Word, PDF, Excel, and 60+ formats) -> converted Markdown
- **Local folders** — point at any directory (OneDrive, project files, etc.)
- Background daemon syncs daily and accepts Telegram commands
- Only changed content is reprocessed on incremental syncs
- Unconvertible files (video, images, binaries) are detected and skipped before download
- Real-time progress bars with ETA during sync

## First-Run Setup

The easiest way to get started — run the setup script:

### macOS / Linux

```bash
git clone https://github.com/visser23/work-context-builder.git
cd work-context-builder
bash setup.sh
```

### Windows (PowerShell)

```powershell
git clone https://github.com/visser23/work-context-builder.git
cd work-context-builder
.\setup.ps1
```

The setup script checks prerequisites, installs dependencies, walks you
through configuration, validates the setup, and optionally runs the first
sync and installs the background daemon.

## Manual Installation

### Prerequisites

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

```bash
git clone https://github.com/visser23/work-context-builder.git
cd work-context-builder
uv sync
```

### Optional dependencies

```bash
# SharePoint browser-based access (cookie capture)
uv pip install playwright
playwright install chromium

# PDF conversion (included by default, but if missing)
uv pip install pymupdf4llm
```

## Quick Start

```bash
# Interactive setup (generates a YAML config)
uv run workctx init

# Store secrets in your OS credential store
uv run workctx auth set my-confluence-pat
uv run workctx auth set my-jira-pat

# Validate everything
uv run workctx doctor --config workctx.yaml

# First sync
uv run workctx sync --config workctx.yaml --full

# Install background daemon (syncs daily, accepts Telegram commands)
uv run workctx install-service --config workctx.yaml
```

## Background Daemon

The daemon runs as a persistent background service. It:

- Syncs automatically once every 24 hours when the machine is on
- Accepts Telegram commands (`/sync`, `/status`, `/help`)
- Restarts automatically if the process crashes
- Starts on login (no manual intervention needed)

### Install the Service

```bash
uv run workctx install-service --config workctx.yaml
```

This installs a platform-appropriate background service:

| Platform | Mechanism |
|---|---|
| macOS | launchd user agent (`KeepAlive`, `RunAtLoad`) |
| Linux | systemd user service |
| Windows | Task Scheduler at-logon trigger |

### Manage the Service

```bash
uv run workctx service-status     # Check if running
uv run workctx remove-service     # Stop and remove
uv run workctx daemon             # Run in foreground (for debugging)
```

### Telegram Commands

If Telegram is configured, send commands to your bot from any device:

| Command | Action |
|---|---|
| `/sync` | Trigger incremental sync now |
| `/syncfull` | Trigger full resync |
| `/status` | Show last sync times and object counts |
| `/help` | List available commands |

## Atlassian Authentication

Supports both **Atlassian Cloud** and **Data Center** instances.

### Cloud (*.atlassian.net)

Uses API tokens with Basic Auth.

1. Go to <https://id.atlassian.com/manage-profile/security/api-tokens>
2. Click **Create API token** -> copy it
3. Store it: `uv run workctx auth set my-cloud-token`

```yaml
sources:
  confluence:
    - name: my-wiki
      base_url: "https://myorg.atlassian.net"
      deployment: cloud
      spaces: [ENG, OPS]
      auth:
        mode: api_token
        username: "you@company.com"
        secret_ref: my-cloud-token
```

### Data Center (self-hosted)

Uses Personal Access Tokens (PATs) with Bearer auth. No username required.

1. Go to **Profile -> Personal Access Tokens** in your DC instance
2. Create a token with read permissions -> copy it
3. Store it: `uv run workctx auth set my-dc-pat`

```yaml
sources:
  confluence:
    - name: my-dc-wiki
      base_url: "https://confluence.myorg.com"
      deployment: datacenter
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

When `deployment` is `"auto"` (default), the tool probes the instance
and detects Cloud vs. Data Center automatically. For reliability, set
`deployment: datacenter` or `deployment: cloud` explicitly.

## SharePoint

Two modes are supported. **Important:** If the SharePoint library is
already synced to your machine via OneDrive, use Mode 1 (local sync).
Only use Mode 2 (browser-based) if the library is **not** synced locally.
Using both for the same library will create duplicate content.

### Mode 1: OneDrive Local Sync (zero config)

If the document library is synced to your machine via OneDrive:

```yaml
sources:
  sharepoint:
    - name: team-docs
      mode: onedrive_local
      local_path: "~/Library/CloudStorage/OneDrive-YourOrg/SharedDocs"   # macOS
      # local_path: "C:/Users/you/OneDrive - YourOrg/SharedDocs"        # Windows
```

### Mode 2: Browser-Based (no local sync needed)

For SharePoint libraries **not synced locally**. Downloads files over
HTTPS via SharePoint's REST API. Unconvertible files (video, images,
binaries >200 MB) are automatically detected from metadata and skipped
before download.

**No Microsoft app registration or admin consent required.**

```yaml
sources:
  sharepoint:
    - name: team-sharepoint
      site_url: "https://company.sharepoint.com/sites/TeamSite"
      mode: browser
      doc_library: "Shared Documents"
      server_relative_path: "/sites/TeamSite/Shared Documents/SubFolder"
      auth:
        mode: browser
        secret_ref: sp-cookies
```

First login: `uv run workctx auth login-sharepoint --source team-sharepoint`

## Local Folders

Point at any local directories (OneDrive folders, project files, shared
drives, etc.) and they'll be scanned recursively.

```yaml
sources:
  local_folders:
    - name: my-projects
      paths:
        - "~/Documents/Projects"
        - "~/Library/CloudStorage/OneDrive-Company/Shared"
      include: ["**/*"]
      exclude: ["**/node_modules/**", "**/.git/**", "**/dist/**"]
```

The scanner automatically skips:
- Its own state database and output directories
- Common junk directories (`.git`, `node_modules`, `__pycache__`, etc.)
- Unconvertible files (images, video, binaries) based on extension
- Hidden files and temp files (`~$*`)

## Supported File Types

Work Context Mirror converts 60+ file formats to Markdown:

| Category | Extensions |
|---|---|
| Office | `.docx`, `.doc`, `.pptx`, `.ppt`, `.xlsx`, `.xls`, `.xlsm`, `.xlsb`, `.rtf` |
| PDF | `.pdf` |
| Email | `.msg`, `.eml` |
| Web | `.html`, `.htm`, `.mhtml`, `.mht` |
| Structured data | `.csv`, `.tsv`, `.json`, `.jsonl`, `.xml` |
| Books / notebooks | `.epub`, `.ipynb` |
| Archives | `.zip` (contents extracted and converted) |
| Code (50+) | `.py`, `.js`, `.ts`, `.java`, `.go`, `.rs`, `.c`, `.cpp`, `.cs`, `.rb`, `.php`, `.swift`, `.sql`, `.sh`, `.ps1`, and many more |
| Markup / docs | `.md`, `.rst`, `.adoc`, `.tex`, `.wiki` |
| Config | `.yaml`, `.toml`, `.ini`, `.env`, `.tf`, `.hcl`, `.dockerfile` |

Unknown text files are auto-detected via heuristic. Binary files that
cannot be converted produce a metadata-only stub with a link to the original.

**Automatically skipped** (never downloaded from SharePoint):
video (`.mov`, `.mp4`), images (`.png`, `.jpg`, `.gif`), audio (`.mp3`,
`.wav`), design files (`.fig`, `.psd`), binaries (`.exe`, `.dmg`), fonts
(`.ttf`, `.woff`), OneNote (`.one`), and anything >200 MB.

## Telegram Notifications

1. Message [@BotFather](https://t.me/BotFather) on Telegram -> `/newbot`
2. Copy the bot token
3. Start a chat with your new bot and send any message
4. Get your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`

```bash
uv run workctx auth set telegram-bot-token
uv run workctx auth set telegram-chat-id
```

```yaml
notifications:
  telegram:
    enabled: true
    bot_token_ref: telegram-bot-token
    chat_id_ref: telegram-chat-id
```

## Secret Storage

| Platform | Backend |
|---|---|
| macOS | Keychain |
| Windows | Windows Credential Locker |
| Linux | Secret Service (GNOME Keyring / KWallet) |

Environment variable fallback: `my-jira-pat` -> `MY_JIRA_PAT`.

## CLI Reference

| Command | Description |
|---|---|
| `workctx init` | Interactive config generator |
| `workctx doctor` | Validate config and environment |
| `workctx sync` | Run incremental sync |
| `workctx sync --full` | Force full resync |
| `workctx status` | Show sync status |
| `workctx search "query"` | Full-text search the corpus |
| `workctx daemon` | Run daemon in foreground |
| `workctx install-service` | Install background daemon |
| `workctx remove-service` | Remove background daemon |
| `workctx service-status` | Check daemon status |
| `workctx auth set <ref>` | Store a secret |
| `workctx auth remove <ref>` | Remove a secret |
| `workctx auth login-sharepoint` | Browser login for SharePoint |
| `workctx install-schedule` | Legacy: fixed-time launchd schedule |

## Output Structure

```
OutputRoot/
+-- README.md             # Auto-generated corpus overview
+-- CONTEXT.md            # Corpus explanation for humans & LLMs
+-- AGENTS.md             # For Codex-style agents
+-- CLAUDE.md             # For Claude Code
+-- _meta/
|   +-- INDEX.md          # Source overview with counts
|   +-- health.json       # Sync health status
|   +-- manifest.jsonl    # Per-document metadata
+-- confluence/<source>/<SPACE>/<page>.md
+-- jira/<source>/<PROJECT>/<PROJ-123>.md
+-- sharepoint/<source>/<path>/<document>.md
+-- local_folder/<source>/<folder>/<file>.md
```

## State & Logs

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/WorkContextMirror/<project-id>/` |
| Windows | `%LOCALAPPDATA%\WorkContextMirror\<project-id>\` |
| Linux | `~/.local/share/WorkContextMirror/<project-id>/` |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Config not found | `--config /path/to/config.yaml` |
| No API token | `workctx auth set <secret_ref>` |
| Lock file stale | Delete `run.lock` in state dir |
| Confluence 401 | Regenerate PAT |
| SharePoint expired | `workctx auth login-sharepoint --source <name>` |
| Daemon not running | `workctx service-status` then `workctx install-service` |

```bash
uv run workctx doctor --config workctx.yaml --verbose
```

## Security Notes

- Secrets stored in **OS credential store only** -- never in config files or logs
- All processing happens **locally** -- nothing sent to external services
- Source systems accessed **read-only**
- SharePoint cookies encrypted in OS credential store

**Important:** Synchronising organisational information into a locally
controlled directory may be restricted by your employer. Ensure
compliance with information-governance requirements.

## Development

```bash
uv sync --extra dev
uv run pytest                    # 105+ tests
uv run ruff check src/ tests/   # Lint
uv run mypy src/                 # Type-check
```

## License

MIT
