# Work Context Builder

**Turn your Confluence, Jira, SharePoint, and local files into a clean
Markdown knowledge base that any AI can read.**

Work Context Mirror syncs your work content into simple Markdown files
on your computer. Once set up, a background daemon keeps everything
fresh — you never think about it again. ChatGPT, Claude, Codex, or any
LLM with filesystem access can then answer questions about your project
using up-to-date context.

```
Confluence  ─┐
Jira        ─┤     Work Context Mirror      ┌── ChatGPT
SharePoint  ─┤──>  (background daemon)  ──>  ├── Codex / Claude Code
Local files ─┘     daily + on-demand         └── Any LLM with file access
                         ▲
                   Telegram: /sync /status
```

**Platforms:** macOS, Windows, Linux &nbsp;|&nbsp; **Requires:** Python 3.12+

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Installation](#installation)
3. [Configuration Guide](#configuration-guide)
   - [Am I on Atlassian Cloud or Data Center?](#am-i-on-atlassian-cloud-or-data-center)
   - [Getting an Atlassian API Token (Cloud)](#getting-an-atlassian-api-token-cloud)
   - [Getting a Personal Access Token (Data Center)](#getting-a-personal-access-token-data-center)
   - [SharePoint Setup](#sharepoint-setup)
   - [Local Folders](#local-folders)
   - [Telegram Notifications (Optional)](#telegram-notifications-optional)
4. [Running Your First Sync](#running-your-first-sync)
5. [Background Daemon](#background-daemon)
6. [Let AI Configure It For You](#let-ai-configure-it-for-you)
7. [CLI Reference](#cli-reference)
8. [Output Structure](#output-structure)
9. [Supported File Types](#supported-file-types)
10. [Troubleshooting](#troubleshooting)
11. [Security](#security)
12. [Development](#development)

---

## What It Does

- **Confluence** pages become individual Markdown files with metadata
- **Jira** issues become Markdown with comments, links, and custom fields
- **SharePoint** documents (Word, Excel, PDF, 60+ formats) are converted to Markdown
- **Local folders** are scanned recursively — point at OneDrive, project directories, anything
- Only changed content is reprocessed (incremental sync — fast after first run)
- Unconvertible files (video, images, binaries) are detected and skipped automatically
- Real-time progress bars so you know how long it'll take

---

## Installation

### Option A: Guided Setup (Recommended)

The setup script checks everything, installs dependencies, walks you
through configuration, and optionally runs the first sync.

**macOS / Linux:**

```bash
git clone https://github.com/visser23/work-context-builder.git
cd work-context-builder
bash setup.sh
```

**Windows (PowerShell):**

```powershell
git clone https://github.com/visser23/work-context-builder.git
cd work-context-builder
.\setup.ps1
```

### Option B: Manual

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/) (a fast Python package manager).

**Install uv** (if you don't have it):

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex
```

**Install the project:**

```bash
git clone https://github.com/visser23/work-context-builder.git
cd work-context-builder
uv sync
```

**Optional — for SharePoint browser mode** (only if you need it):

```bash
uv pip install playwright
uv run playwright install chromium
```

---

## Configuration Guide

All configuration lives in **one YAML file**. You can either:

- Run `uv run workctx init` for an interactive wizard, or
- Copy `example-config.yaml` to `workctx.yaml` and edit it by hand

The YAML file tells Work Context Mirror:
- Where your sources are (Confluence URL, Jira URL, SharePoint, folders)
- How to authenticate (token references — the actual secrets are stored safely in your OS credential store, never in the YAML)
- Where to put the output Markdown files

Below is a complete walkthrough of every setting you might need.

### Am I on Atlassian Cloud or Data Center?

This is the most common source of confusion. Here's how to tell:

| Check | Cloud | Data Center |
|---|---|---|
| **Your URL** | `https://yourcompany.atlassian.net` | `https://confluence.yourcompany.com` (or any custom domain) |
| **Who hosts it** | Atlassian (in their cloud) | Your company (on their own servers) |
| **Login page** | Atlassian ID (id.atlassian.com) | Company SSO or built-in login |
| **Admin access** | You manage it at admin.atlassian.com | Your IT team manages the server |

**Still not sure?** Look at the URL in your browser when you're on Confluence or Jira:
- If it contains `.atlassian.net` → **Cloud**
- If it's anything else (your company's domain) → **Data Center**

You can set `deployment: "auto"` in config and the tool will detect it for you, but it's more reliable to set it explicitly.

### Getting an Atlassian API Token (Cloud)

If your Confluence/Jira URL contains `.atlassian.net`, you need an **API token**.

1. Go to <https://id.atlassian.com/manage-profile/security/api-tokens>
2. Click **"Create API token"**
3. Give it a label (e.g. "Work Context Mirror") and click **Create**
4. **Copy the token** — you can only see it once!
5. Store it:

```bash
uv run workctx auth set my-confluence-token
# It will prompt you to paste the token (hidden input)
```

Your config will look like:

```yaml
sources:
  confluence:
    - name: my-wiki
      base_url: "https://yourcompany.atlassian.net"
      deployment: cloud          # or "auto"
      spaces: [ENG, PROJ]       # space keys from Confluence
      auth:
        mode: api_token
        username: "you@yourcompany.com"   # your Atlassian login email
        secret_ref: my-confluence-token   # must match what you used above

  jira:
    - name: my-jira
      base_url: "https://yourcompany.atlassian.net"
      deployment: cloud
      projects: [PROJ, OPS]     # project keys from Jira
      auth:
        mode: api_token
        username: "you@yourcompany.com"
        secret_ref: my-jira-token
```

> **Where do I find space/project keys?**
> - **Confluence space key**: look at the URL when you're in a space. It's the short code
>   in the URL, like `https://yourcompany.atlassian.net/wiki/spaces/ENG/...` → key is `ENG`
> - **Jira project key**: the prefix on issue numbers, like `PROJ-123` → key is `PROJ`

### Getting a Personal Access Token (Data Center)

If your Confluence/Jira is self-hosted (not `.atlassian.net`), you need a **Personal Access Token (PAT)**.

**For Confluence Data Center:**

1. Log into your Confluence instance
2. Click your **profile picture** (top right) → **Settings** (or **Profile**)
3. In the left sidebar, click **Personal Access Tokens**
4. Click **Create token**
5. Give it a name, set permissions to **Read** (that's all we need)
6. Click **Create** and **copy the token**
7. Store it:

```bash
uv run workctx auth set my-dc-confluence-pat
```

> **Can't find Personal Access Tokens?** Your admin may need to enable
> it. It's under **Administration → General Configuration → Personal Access Tokens**.
> If it's truly not available, ask your admin or use `mode: basic` with
> your username and password instead.

**For Jira Data Center:** Same process — Profile → Personal Access Tokens → Create.

Your config:

```yaml
sources:
  confluence:
    - name: my-dc-wiki
      base_url: "https://confluence.yourcompany.com"
      deployment: datacenter
      spaces: [PROJ, TEAM]
      auth:
        mode: pat                         # no username needed for PAT
        secret_ref: my-dc-confluence-pat

  jira:
    - name: my-dc-jira
      base_url: "https://jira.yourcompany.com"
      deployment: datacenter
      projects: [PROJ, OPS]
      auth:
        mode: pat
        secret_ref: my-dc-jira-pat
      include_comments: true
```

### SharePoint Setup

There are **two modes**. Pick the one that matches your situation:

#### Do I have the SharePoint library synced to my computer?

Open Finder (macOS) or File Explorer (Windows). Look for your OneDrive
folders. If you can see the SharePoint files as regular folders on your
computer, they're **locally synced** — use Mode 1.

If you can only access the files through a browser at
`yourcompany.sharepoint.com`, they're **not locally synced** — use Mode 2.

> **Important:** Don't use both modes for the same library — you'll get
> duplicate content.

#### Mode 1: OneDrive Local Sync (easiest, preferred)

No authentication needed. Just point at the folder:

```yaml
sources:
  sharepoint:
    - name: team-docs
      mode: onedrive_local
      local_path: "~/Library/CloudStorage/OneDrive-YourCompany/Documents"
      # Windows users: "C:/Users/YourName/OneDrive - YourCompany/Documents"
```

> **How to find your OneDrive path:**
> - **macOS**: Open Finder → look in the sidebar under "Locations" for
>   your OneDrive folder. Right-click it → "Get Info" to see the full path.
>   It's usually `~/Library/CloudStorage/OneDrive-CompanyName/...`
> - **Windows**: Open File Explorer → look for "OneDrive - CompanyName"
>   in the sidebar. Right-click → Properties to see the path.

#### Mode 2: Browser-Based (no local sync needed)

For SharePoint libraries you can only access in a browser. The tool opens
a browser window once for you to log in, captures the session cookies,
and then uses SharePoint's REST API to download files.

**No Microsoft app registration or admin consent required.**

```yaml
sources:
  sharepoint:
    - name: team-sharepoint
      site_url: "https://yourcompany.sharepoint.com/sites/YourSite"
      mode: browser
      doc_library: "Shared Documents"
      server_relative_path: "/sites/YourSite/Shared Documents"
      auth:
        mode: browser
        secret_ref: sp-cookies
```

Then run:

```bash
uv run workctx auth login-sharepoint --source team-sharepoint
```

A browser window opens. Log in as normal. Once you're in, the tool
captures the session cookies automatically. You'll need to re-run this
if the cookies expire (the tool will tell you when).

> **Where do I find `site_url` and `server_relative_path`?**
> - Go to the SharePoint document library in your browser
> - `site_url` is the part up to the site name:
>   `https://yourcompany.sharepoint.com/sites/YourSite`
> - `server_relative_path` is the folder path on the server:
>   `/sites/YourSite/Shared Documents` (or `/sites/YourSite/Shared Documents/SubFolder`)
> - `doc_library` is usually `"Shared Documents"` (the default SharePoint library name)

### Local Folders

Point at any directories on your computer and they'll be scanned recursively:

```yaml
sources:
  local_folders:
    - name: project-files
      paths:
        - "~/Documents/Projects"
        - "~/Desktop/Notes"
      exclude:
        - "**/node_modules/**"
        - "**/.git/**"
        - "**/dist/**"
```

The tool automatically skips:
- Its own output and state directories (no infinite loops)
- Common junk: `.git`, `node_modules`, `__pycache__`, `.venv`, etc.
- Files it can't convert (images, video, binaries)

### Telegram Notifications (Optional)

Get notified on your phone when syncs fail, and trigger syncs remotely.

**Setting up the bot (takes 2 minutes):**

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Follow the prompts — give your bot a name and username
4. BotFather gives you a **bot token** (looks like `123456789:ABCdefGHI...`)
5. Copy it and store it:

```bash
uv run workctx auth set my-telegram-bot
# Paste the bot token when prompted
```

6. Now **open a chat with your new bot** in Telegram and send it any message (like "hello")
7. Open this URL in your browser (replace `<TOKEN>` with your actual bot token):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
8. Look for `"chat":{"id":123456789` — that number is your **chat ID**
9. Store it:

```bash
uv run workctx auth set my-telegram-chat
# Enter the chat ID number when prompted
```

10. Add to your config:

```yaml
notifications:
  telegram:
    enabled: true
    bot_token_ref: my-telegram-bot
    chat_id_ref: my-telegram-chat
```

Once the daemon is running, you can send these commands to your bot:

| Command | What it does |
|---|---|
| `/sync` | Trigger an incremental sync right now |
| `/syncfull` | Trigger a full resync of everything |
| `/status` | Show when each source last synced and how many objects |
| `/help` | List available commands |

---

## Running Your First Sync

After configuration, validate everything works:

```bash
uv run workctx doctor             # checks config, auth, connectivity
```

If doctor is happy, run the first sync:

```bash
uv run workctx sync --full        # downloads everything for the first time
```

The first run can take a while (minutes to hours depending on how much
content you have). You'll see progress bars with estimated time remaining.
After that, incremental syncs only process what changed and take seconds.

---

## Background Daemon

Once you're happy the first sync worked, install the daemon:

```bash
uv run workctx install-service
```

This sets up a background service that:
- **Starts automatically** when you log in
- **Syncs once a day** (picks an opportune time)
- **Accepts Telegram commands** if configured
- **Restarts itself** if it crashes

| Platform | How it works |
|---|---|
| macOS | launchd user agent (KeepAlive + RunAtLoad) |
| Linux | systemd user service |
| Windows | Task Scheduler at-logon trigger |

**Managing the daemon:**

```bash
uv run workctx service-status     # is it running?
uv run workctx remove-service     # stop and uninstall
uv run workctx daemon             # run in foreground for debugging
```

---

## Let AI Configure It For You

If you use ChatGPT, Claude, Cursor, or any AI assistant that can run
commands on your computer, paste this prompt and let it do the work:

> **Prompt to give your AI assistant:**
>
> I've cloned the Work Context Mirror repo at `[path to repo]`.
> I need you to configure it for my setup:
>
> - My Confluence is at: `[your Confluence URL]`
> - My Jira is at: `[your Jira URL]`
> - Confluence spaces I need: `[space keys, e.g. ENG, PROJ]`
> - Jira projects I need: `[project keys, e.g. PROJ, OPS]`
> - My Atlassian login email: `[your email]`
> - I have an API token / PAT ready: `[yes/no — if no, tell me how to get one]`
> - SharePoint: `[URL or "not needed" or "it's synced to my OneDrive at [path]"]`
> - Telegram: `[bot token and chat ID, or "not needed"]`
> - I want the output in: `[folder path, e.g. ~/Documents/WorkContext]`
>
> Please:
> 1. Read the README.md and example-config.yaml
> 2. Create a workctx.yaml config file for my setup
> 3. Store my secrets using `uv run workctx auth set ...`
> 4. Run `uv run workctx doctor` to validate
> 5. Run `uv run workctx sync --full` for the first sync
> 6. Install the background daemon with `uv run workctx install-service`

Fill in the blanks and the AI will handle the rest.

---

## CLI Reference

Every command supports `--help` for details. Prefix with `uv run` when
running from the repo directory.

| Command | What it does |
|---|---|
| `workctx init` | Interactive config wizard — asks questions, writes YAML |
| `workctx doctor` | Validates config, checks auth, tests connectivity |
| `workctx sync` | Incremental sync (only changes since last run) |
| `workctx sync --full` | Full sync (reprocesses everything) |
| `workctx status` | Shows per-source sync times and object counts |
| `workctx search "query"` | Full-text search across the entire corpus |
| `workctx daemon` | Run the daemon in the foreground (for debugging) |
| `workctx install-service` | Install background daemon (auto-starts on login) |
| `workctx remove-service` | Stop and remove the background daemon |
| `workctx service-status` | Check if the daemon is running |
| `workctx auth set <ref>` | Store a secret (token, password, etc.) |
| `workctx auth remove <ref>` | Delete a stored secret |
| `workctx auth login-sharepoint` | Browser login for SharePoint cookie capture |
| `workctx reconcile` | Force deletion detection across all sources |
| `workctx reindex` | Rebuild the full-text search index |

---

## Output Structure

```
<output_root>/
├── CONTEXT.md                    Corpus overview for humans & LLMs
├── AGENTS.md                     Guidance for Codex-style agents
├── CLAUDE.md                     Guidance for Claude Code
├── README.md                     Auto-generated summary
├── _meta/
│   ├── INDEX.md                  Source overview with counts
│   ├── health.json               Sync health status
│   └── manifest.jsonl            Per-document metadata
├── confluence/<source>/<space>/<page>.md
├── jira/<source>/<project>/<ISSUE-KEY>.md
├── sharepoint/<source>/<path>/<document>.md
└── local_folder/<source>/<dir>/<file>.md
```

Every Markdown file has YAML front matter with full provenance:

```yaml
---
source_type: confluence
source_name: my-wiki
title: "Architecture Overview"
source_url: "https://..."
updated_at: "2026-08-15T10:30:00Z"
synced_at: "2026-09-01T05:00:00Z"
content_sha256: "abc123..."
---
```

---

## Supported File Types

**Converted to Markdown (60+ formats):**

| Category | Extensions |
|---|---|
| Office | `.docx`, `.doc`, `.pptx`, `.ppt`, `.xlsx`, `.xls`, `.xlsm`, `.xlsb`, `.rtf` |
| PDF | `.pdf` |
| Email | `.msg`, `.eml` |
| Web | `.html`, `.htm`, `.mhtml` |
| Data | `.csv`, `.tsv`, `.json`, `.jsonl`, `.xml` |
| Books / Notebooks | `.epub`, `.ipynb` |
| Archives | `.zip` (contents extracted) |
| Code (50+) | `.py`, `.js`, `.ts`, `.java`, `.go`, `.rs`, `.c`, `.cpp`, `.cs`, `.rb`, `.php`, `.swift`, `.sql`, `.sh`, `.ps1`, `.yaml`, `.toml`, and many more |
| Markup | `.md`, `.rst`, `.adoc`, `.tex`, `.wiki` |
| Config | `.ini`, `.env`, `.tf`, `.hcl`, `.dockerfile` |

**Automatically skipped** (detected from metadata, never downloaded):
video (`.mov`, `.mp4`, `.avi`), images (`.png`, `.jpg`, `.gif`, `.svg`),
audio (`.mp3`, `.wav`), design (`.fig`, `.psd`, `.sketch`), binaries
(`.exe`, `.dmg`, `.msi`), fonts (`.ttf`, `.woff`), OneNote (`.one`),
and anything over 200 MB.

Files that can't be converted produce a metadata-only stub with a link
to the original.

---

## Troubleshooting

**First step — always run doctor:**

```bash
uv run workctx doctor --verbose
```

This checks your config, verifies auth tokens work, tests connectivity
to each source, and reports exactly what's wrong.

**Common issues:**

| Problem | What to do |
|---|---|
| `Config file not found` | Pass `--config path/to/your-config.yaml` to every command |
| `401 Unauthorized` on Confluence/Jira | Your token expired or is wrong. Generate a new one and `workctx auth set <ref>` |
| `SharePoint session expired` | Run `workctx auth login-sharepoint --source <name>` again |
| `Lock file stale` | Another sync crashed. Delete `run.lock` from the state directory |
| `Daemon not running` | Run `workctx service-status`, then `workctx install-service` to reinstall |
| First sync is slow | Normal — it downloads everything. Check progress bars for ETA. Subsequent syncs are fast. |
| `No results` from search | Run `workctx reindex` to rebuild the search index |

**Where are state files and logs?**

| Platform | Default path |
|---|---|
| macOS | `~/Library/Application Support/WorkContextMirror/<project-id>/` |
| Windows | `%LOCALAPPDATA%\WorkContextMirror\<project-id>\` |
| Linux | `~/.local/share/WorkContextMirror/<project-id>/` |

You can override this with `state_dir` in your config.

---

## Security

- Secrets (tokens, passwords) are stored in your **OS credential store**
  (Keychain on macOS, Credential Locker on Windows, Secret Service on Linux)
  — never in config files, never in logs
- Environment variable fallback: `my-jira-pat` is looked up as `MY_JIRA_PAT`
- All processing happens **locally on your machine** — no content is
  sent to any external service
- Source systems are accessed **read-only** — the tool never creates,
  modifies, or deletes anything in Confluence, Jira, or SharePoint
- A log filter prevents secrets from appearing in log files

> **Heads up:** Synchronising organisational information to a locally
> controlled directory may be subject to your employer's information
> governance policies. Check before you set this up on work content.

---

## Development

```bash
uv sync --extra dev
uv run pytest                    # 138 tests
uv run ruff check src/ tests/   # lint
uv run ruff format src/ tests/  # format
```

Contributions welcome. The architecture is documented in `docs/`.

---

## License

MIT
