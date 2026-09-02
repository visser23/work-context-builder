# Work Context Mirror

Cross-platform tool that creates and maintains a local, LLM-friendly
mirror of work knowledge from Confluence, Jira, SharePoint, and local
folders.

Configure a project once and a background daemon keeps your knowledge
corpus continuously refreshed. ChatGPT, Codex, Claude Code, or any
LLM with filesystem access can then interrogate current project context
directly from clean Markdown files.

**Platforms:** macOS, Windows, Linux &nbsp;|&nbsp; **Runtime:** Python 3.12+

## How It Works

```
Confluence  ─┐
Jira        ─┤     Work Context Mirror      ┌── ChatGPT
SharePoint  ─┤──>  (background daemon)  ──>  ├── Codex
Local files ─┘     daily + on-demand         └── Claude Code
                         ▲
                   Telegram: /sync /status
```

- **Confluence** pages become individual Markdown files with full metadata
- **Jira** issues become Markdown with comments, links, and custom fields
- **SharePoint** documents (Word, Excel, PDF, 60+ formats) are converted to Markdown
- **Local folders** are scanned recursively (OneDrive, project directories, etc.)
- A background daemon syncs daily; trigger on-demand via Telegram or CLI
- Only changed content is reprocessed (incremental sync)
- Unconvertible files (video, images, binaries) are detected and skipped before download

## Quick Start

### 1. Install

```bash
git clone https://github.com/visser23/work-context-builder.git
cd work-context-builder
uv sync
```

Or use the guided setup script:

```bash
bash setup.sh        # macOS / Linux
.\setup.ps1          # Windows (PowerShell)
```

### 2. Configure

```bash
uv run workctx init                 # interactive config generator
uv run workctx auth set my-token    # store secret in OS credential store
uv run workctx doctor               # validate everything
```

Or copy `example-config.yaml`, edit it, and store your secrets manually.

### 3. Sync

```bash
uv run workctx sync --full          # initial full sync
uv run workctx sync                 # subsequent incremental syncs
```

### 4. Install Background Daemon

```bash
uv run workctx install-service      # auto-syncs daily, Telegram commands
```

| Platform | Mechanism |
|---|---|
| macOS | launchd user agent (KeepAlive, RunAtLoad) |
| Linux | systemd user service |
| Windows | Task Scheduler at-logon trigger |

## Configuration

All configuration lives in a single YAML file. See
[`example-config.yaml`](example-config.yaml) for a fully commented
template.

```yaml
version: 1

project:
  id: "my-project"
  name: "My Project"
  output_root: "~/Documents/WorkContext/my-project"
  # state_dir: "~/Documents/WorkContext/my-project/_state"  # optional override

sources:
  confluence:
    - name: my-wiki
      base_url: "https://company.atlassian.net"
      spaces: [ENG, OPS]
      auth:
        mode: api_token
        username: "you@company.com"
        secret_ref: my-confluence-token

  jira:
    - name: my-jira
      base_url: "https://company.atlassian.net"
      projects: [PROJ, OPS]
      auth:
        mode: api_token
        username: "you@company.com"
        secret_ref: my-jira-token
```

## Atlassian Authentication

Supports both **Atlassian Cloud** (`.atlassian.net`) and **Data Center**
(self-hosted) instances. Set `deployment: auto` (default) for
auto-detection, or `cloud` / `datacenter` explicitly.

### Cloud

1. Go to <https://id.atlassian.com/manage-profile/security/api-tokens>
2. Create an API token
3. `uv run workctx auth set my-confluence-token`

Config: `mode: api_token`, `username: you@company.com`

### Data Center

1. Go to **Profile > Personal Access Tokens** in your DC instance
2. Create a token with read permissions
3. `uv run workctx auth set my-dc-pat`

Config: `mode: pat` (no username needed)

## SharePoint

Two modes. **Use only one per library to avoid duplicates.**

### Mode 1: OneDrive Local Sync (preferred)

If the SharePoint library is already synced to your machine via OneDrive:

```yaml
sharepoint:
  - name: team-docs
    mode: onedrive_local
    local_path: "~/Library/CloudStorage/OneDrive-Company/SharedDocs"  # macOS
    # local_path: "C:/Users/you/OneDrive - Company/SharedDocs"       # Windows
```

No browser automation or app registration required.

### Mode 2: Browser-Based

For libraries **not** synced locally. Downloads via SharePoint's REST API
using browser session cookies. No Microsoft app registration or admin
consent required.

```yaml
sharepoint:
  - name: team-sharepoint
    site_url: "https://company.sharepoint.com/sites/TeamSite"
    mode: browser
    doc_library: "Shared Documents"
    server_relative_path: "/sites/TeamSite/Shared Documents"
    auth:
      mode: browser
      secret_ref: sp-cookies
```

First login (opens a browser window):

```bash
uv run workctx auth login-sharepoint --source team-sharepoint
```

Requires Playwright: `uv pip install playwright && playwright install chromium`

## Local Folders

Scan any local directories recursively:

```yaml
local_folders:
  - name: my-projects
    paths:
      - "~/Documents/Projects"
      - "~/OneDrive/Shared"
    exclude: ["**/node_modules/**", "**/.git/**"]
```

Automatically skips its own state/output directories, common junk
directories, hidden files, and unconvertible files.

## Supported File Types

**Converted to Markdown (60+ formats):**

| Category | Extensions |
|---|---|
| Office | `.docx`, `.doc`, `.pptx`, `.ppt`, `.xlsx`, `.xls`, `.xlsm`, `.xlsb`, `.rtf` |
| PDF | `.pdf` |
| Email | `.msg`, `.eml` |
| Web | `.html`, `.htm`, `.mhtml` |
| Data | `.csv`, `.tsv`, `.json`, `.jsonl`, `.xml` |
| Books | `.epub`, `.ipynb` |
| Code | `.py`, `.js`, `.ts`, `.java`, `.go`, `.rs`, `.c`, `.cpp`, `.rb`, `.php`, `.swift`, `.sql`, `.sh`, `.ps1`, and 40+ more |
| Markup | `.md`, `.rst`, `.adoc`, `.tex`, `.wiki` |
| Config | `.yaml`, `.toml`, `.ini`, `.env`, `.tf`, `.hcl`, `.dockerfile` |

**Automatically skipped** (never downloaded from SharePoint):
video, images, audio, design files, binaries, fonts, OneNote, and
anything over 200 MB.

## Background Daemon

The daemon runs as a persistent background service:

- Syncs automatically once every 24 hours
- Accepts Telegram commands (`/sync`, `/syncfull`, `/status`, `/help`)
- Restarts automatically on crash
- Starts on login

```bash
uv run workctx install-service    # install
uv run workctx service-status     # check
uv run workctx remove-service     # remove
uv run workctx daemon             # run in foreground (debugging)
```

## Telegram

Optional. Receive failure alerts and trigger syncs from any device.

1. Message [@BotFather](https://t.me/BotFather) > `/newbot`
2. Copy the bot token
3. Send any message to your new bot
4. Get your chat ID: `https://api.telegram.org/bot<TOKEN>/getUpdates`

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
| Windows | Credential Locker |
| Linux | Secret Service (GNOME Keyring / KWallet) |

Fallback: environment variables (`my-jira-pat` -> `MY_JIRA_PAT`).

## CLI Reference

```
workctx init                    Interactive config generator
workctx doctor                  Validate config and environment
workctx sync [--full]           Run sync (incremental or full)
workctx status                  Show sync status per source
workctx search "query"          Full-text search the corpus
workctx daemon                  Run daemon in foreground
workctx install-service         Install background daemon
workctx remove-service          Remove background daemon
workctx service-status          Check daemon status
workctx auth set <ref>          Store a secret
workctx auth remove <ref>       Remove a secret
workctx auth login-sharepoint   Browser login for SharePoint
workctx reconcile               Force deletion detection
workctx reindex                 Rebuild FTS5 search index
```

## Output Structure

```
<output_root>/
├── CONTEXT.md                    # Corpus explanation for humans & LLMs
├── AGENTS.md                     # Codex-style agent guidance
├── CLAUDE.md                     # Claude Code guidance
├── README.md                     # Auto-generated overview
├── _meta/
│   ├── INDEX.md                  # Source overview with counts
│   ├── health.json               # Sync health status
│   └── manifest.jsonl            # Per-document metadata
├── confluence/<source>/<space>/<page>.md
├── jira/<source>/<project>/<ISSUE-KEY>.md
├── sharepoint/<source>/<path>/<document>.md
└── local_folder/<source>/<dir>/<file>.md
```

Every Markdown file includes YAML front matter with full provenance:
`source_type`, `source_url`, `updated_at`, `synced_at`, `source_version`,
`content_sha256`.

## State & Logs

State is stored per-project in a platform-specific location (overridable
via `state_dir` in config):

| Platform | Default path |
|---|---|
| macOS | `~/Library/Application Support/WorkContextMirror/<id>/` |
| Windows | `%LOCALAPPDATA%\WorkContextMirror\<id>\` |
| Linux | `~/.local/share/WorkContextMirror/<id>/` |

Contains: `state.sqlite` (source objects, checkpoints, FTS5 index),
`logs/` (rotating log files), `run.lock` (execution lock).

## Troubleshooting

```bash
uv run workctx doctor --verbose   # comprehensive environment check
```

| Problem | Fix |
|---|---|
| Config not found | Use `--config /path/to/config.yaml` |
| Auth 401 | Regenerate token, `workctx auth set <ref>` |
| SharePoint expired | `workctx auth login-sharepoint --source <name>` |
| Lock file stale | Delete `run.lock` in state dir |
| Daemon not running | `workctx service-status`, then `workctx install-service` |

## Security

- Secrets stored in **OS credential store** only — never in config files or logs
- All processing happens **locally** — no content sent to external services
- Source systems accessed **read-only**
- Log filter prevents secrets from appearing in log files

> **Note:** Synchronising organisational information may be subject to
> your employer's information-governance policies. Ensure compliance
> before use.

## Development

```bash
uv sync --extra dev
uv run pytest                    # 138 tests
uv run ruff check src/ tests/   # lint
uv run ruff format src/ tests/  # format
```

## License

MIT
