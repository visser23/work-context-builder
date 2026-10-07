# Work Context Builder

**Turn your Confluence, Jira, SharePoint, Teams meeting transcripts, email,
calendar, Slack and local files into a clean Markdown knowledge base that any
AI can read.**

Work Context Mirror syncs your work content into simple Markdown files
on your computer. Once set up, a background daemon keeps everything
fresh — you never think about it again. ChatGPT, Claude, Codex, or any
LLM with filesystem access can then answer questions about your project
using up-to-date context.

```
Confluence  ─┐
Jira        ─┤
SharePoint  ─┤     Work Context Mirror      ┌── ChatGPT
Teams       ─┤──>  (background daemon)  ──>  ├── Codex / Claude Code
 transcripts ─┤     daily + on-demand         └── Any LLM with file access
Email       ─┤
Calendar    ─┤
Slack       ─┤
Local files ─┘
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
   - [Teams Meeting Transcripts](#teams-meeting-transcripts)
   - [Email, Calendar and Slack](#email-calendar-and-slack)
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
- **Teams meeting transcripts** (yours and those shared with you) become searchable Markdown with speaker names and timestamps — no app registration needed
- **Email** (Outlook / Exchange Online inbox + sent items), your **calendar** (past and upcoming events, attendees, join links) and **Slack** (channels, DMs, threads — one digest per conversation per day) are pulled daily and indexed — no app registration, tokens or admin consent needed
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
git clone https://github.com/youruser/work-context-builder.git
cd work-context-builder
bash setup.sh
```

**Windows (PowerShell):**

```powershell
git clone https://github.com/youruser/work-context-builder.git
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
git clone https://github.com/youruser/work-context-builder.git
cd work-context-builder
uv sync
```

**Required for SharePoint browser mode:**

If you use SharePoint with `mode: browser` (the default for SharePoint sources
that aren't synced locally via OneDrive), you **must** install Playwright and
Chromium. The daemon uses Playwright to refresh session cookies automatically:

```bash
uv sync --extra playwright
uv run playwright install chromium
```

> **Note:** The background daemon service also needs Playwright. When you run
> `uv run workctx install-service`, it now automatically includes `--extra playwright`
> so the daemon's virtual environment has Playwright installed.

---

## Configuration Guide

All configuration lives in **one YAML file** named **`workctx.yaml`** (by convention).
You can either:

- Run `uv run workctx init` for an interactive wizard, or
- Copy `example-config.yaml` to `workctx.yaml` and edit it by hand

> **Config resolution:** The CLI looks for `workctx.yaml` (or `workctx.yml`)
> first. If that doesn't exist, it picks the only YAML file in the current
> directory. If multiple non-example YAML files exist, pass `--config <path>`.

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
      doc_library: "Documents"              # see note below
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
captures the session cookies automatically.

> **Cookie expiry & automatic refresh:** SharePoint invalidates its session
> cookies (`rtFa`/`FedAuth`) on the server after a few hours, even though the
> copies in the browser profile still look alive for days. The tool's login
> profile (see `~/Library/Application Support/WorkContextMirror/browser-profiles/`)
> keeps a long-lived identity-provider cookie (Entra `ESTSAUTHPERSISTENT`, ~90 days),
> which lets a **headless browser silently re-authenticate** in a few seconds.
>
> - Before every sync, and every 4 hours in the daemon, stored cookies are
>   tested against SharePoint. If rejected, the headless refresh runs and waits
>   up to 45 s for the silent SSO redirect to hand back fresh cookies. Only
>   cookies that pass an HTTP check are saved.
> - You only get a Telegram alert (*"SharePoint session expired … Automatic
>   refresh failed"*) when the identity provider genuinely needs you — a
>   password/MFA prompt, or the ~90-day login cookie lapsing. Then run the
>   `workctx-relogin` command from the message.
> - `workctx-relogin` also validates what it captures; it will not store cookies
>   SharePoint rejects.

> **Where do I find `site_url` and `server_relative_path`?**
> - Go to the SharePoint document library in your browser
> - `site_url` is the part up to the site name:
>   `https://yourcompany.sharepoint.com/sites/YourSite`
> - `server_relative_path` is the folder path on the server:
>   `/sites/YourSite/Shared Documents` (or `/sites/YourSite/Shared Documents/SubFolder`)
> - `doc_library` is the **list title** in SharePoint (often `"Documents"` or `"Shared Documents"` —
>   the easiest way to check is to go to Site Contents in the browser and look at the library name).
>   The folder path in the URL (`Shared Documents`) and the list title can be different!

### Teams Meeting Transcripts

Pulls the transcripts of every Teams meeting you recorded or that was
shared with you into a `transcripts/` folder, and adds them to the
full-text search index. It **reuses your SharePoint browser login** — no
Azure app registration, no admin consent, no extra token.

**How it works:** when a Teams meeting is recorded with transcription on,
the transcript is stored with the `.mp4` recording (in the organiser's
OneDrive `Recordings` folder, or a team site). The tool finds those
recordings with SharePoint Search, then downloads each transcript through
SharePoint's media API using your existing session cookies.

**Prerequisites:**

1. A SharePoint source in **browser mode** (see [SharePoint Setup](#sharepoint-setup))
   that you have already logged in to (`workctx auth login-sharepoint`).
2. Playwright installed (`uv sync --extra playwright && uv run playwright install chromium`).
   OneDrive lives on a second host (`<tenant>-my.sharepoint.com`); its
   cookies are obtained automatically and silently from the same browser
   profile the first time they are needed.

**Config:**

```yaml
sources:
  sharepoint:
    - name: my-sharepoint            # your existing browser-mode source
      site_url: "https://contoso.sharepoint.com/sites/MyTeam"
      # ...
      auth:
        mode: browser
        secret_ref: my-sharepoint-cookies

  transcripts:
    - name: teams-transcripts
      sharepoint_source: my-sharepoint   # which login to reuse
      # Optional:
      # include_own: true                # meetings you organised (default)
      # include_shared: true             # recordings shared directly with you (default)
      # include_invited: true            # EVERY meeting you were invited to (default)
      # include_all_sites: false         # also org-wide sites you can only read
      # sites:                           # extra site URLs whose recordings are always included
      #   - "https://contoso.sharepoint.com/sites/MyTeam"
      # since_days: 365                  # only meetings from the last N days
      # exclude_titles: ["*1:1*", "*HR*"]  # case-insensitive globs on the meeting title
```

**Which meetings are included?** SharePoint Search only returns recordings
your account can open, and Teams only grants access to people who were
invited. With `include_invited: true` (the default) the tool takes that
tenant-wide list and keeps:

| Where the recording lives | Included when |
|---|---|
| Your own OneDrive | always (`include_own`) |
| Someone else's OneDrive | you can open it — Teams only shares these with invited participants |
| A Teams team site (`/sites/msteams_*`) | you can open it (channel meetings) |
| Any other SharePoint site | you are a member/contributor of that site (this includes your project site) |
| Read-only sites (e.g. organisation-wide webinars, directorate sites) | only with `include_all_sites: true` |

Expect hundreds or thousands of meetings on a first sync in a large
organisation (a real-world run: ~1,600 transcripts, ~90 MB, ~8 minutes;
later runs take ~30 seconds). Use `since_days` to limit the history.

**Output** (one file per meeting):

```
transcripts/<source>/<YYYY>/<YYYY-MM-DD>-<meeting-title>-<id>.md
```

Each file has front matter (`meeting_date`, `duration_minutes`,
`participants`) and a body of `[HH:MM:SS] **Speaker:** text` turns.
Consecutive lines from one speaker are merged into paragraphs.

**Run only this source:** `uv run workctx sync --source teams-transcripts`

> **Privacy:** this includes *every* meeting you were invited to that was
> recorded and transcribed — including other people's 1:1s that you were in.
> Use `exclude_titles`, `since_days` and `include_invited: false` to narrow
> it, and check your employer's information governance policy first. Cookies are only ever sent to
> your own tenant's two SharePoint hosts.

### Email, Calendar and Slack

Pulls your day-to-day working context — what people wrote to you, what is in
your diary, what was said in chat — into `email/`, `calendar/` and `slack/`
folders, all full-text indexed like everything else. Like the transcripts
source it **reuses the browser profile that already holds your single-sign-on
session**, so there is no Azure app registration, no Slack app, no API token
and no admin consent.

**How it works:** the sync opens that profile in a headless browser, reads the
access token Outlook on the web (or the Slack web client) already holds, and
closes the browser. The credentials stay **in memory only** — they are never
written to disk, the keychain or the logs — and are used for ordinary read-only
API calls to your own Outlook / Slack host. If the single-sign-on session has
lapsed the browser signs in silently; if a human is needed, run
`uv run workctx auth login-web --source <name>` once.

**Prerequisites:** Playwright (`uv sync --extra playwright && uv run playwright
install chromium`) and a browser profile that is signed in to the app. The
easiest route is a `mode: browser` SharePoint source (see
[SharePoint Setup](#sharepoint-setup)) in the same Microsoft account — point
`sharepoint_source` at it. Otherwise use `profile: <name>` and run
`workctx auth login-web --source <name>`.

```yaml
sources:
  mail:
    - name: work-mail
      sharepoint_source: my-sharepoint     # or: profile: my-profile
      # folders: ["inbox", "sentitems"]    # well-known names or your own folder names
      # since_days: 90                     # rolling window (3650 = everything); older mail already synced is kept
      # exclude_senders: ["noreply@*"]     # globs on address or display name
      # exclude_subjects: ["automatic reply*"]
      # trim_quoted_replies: true          # cut the quoted history below a reply
      # max_body_chars: 20000

  calendar:
    - name: work-calendar
      sharepoint_source: my-sharepoint
      # past_days: 90
      # future_days: 60
      # include_cancelled: false
      # exclude_titles: ["lunch", "focus time*"]

  slack:
    - name: work-slack
      sharepoint_source: my-sharepoint
      client_url: "https://app.slack.com/client/E0XXXXXXXXX/"   # copy from your browser's address bar
      # signin_url: "https://my-org.enterprise.slack.com/"      # lets a lapsed session sign in silently via SSO
      # since_days: 30
      # conversation_types: ["public_channel", "private_channel", "mpim", "im"]
      # workspaces: ["my-workspace"]       # default: every workspace you belong to
      # include_channels: ["team-*"]       # globs; default: all conversations you are in
      # exclude_channels: ["random", "*-alerts"]
      # include_threads: true
```

**Output:**

```
email/<source>/<YYYY>/<MM>/<date>-<subject>-<id>.md                one file per message
calendar/<source>/<YYYY>/<MM>/<date>-<title>-<id>.md               one file per event
slack/<source>/<workspace>/<conversation>-<id>/<YYYY-MM-DD>.md     one digest per day
```

Email files carry `sender`, `folder` and `participants` front matter and the
message body (HTML flattened to text, quoted history trimmed, attachments
listed by name). Calendar files carry `start_at`, `end_at`, `location`,
`organizer` and `participants`, plus the attendee responses and Teams join
link. Slack digests carry `workspace`, `channel`, `message_count` and
`participants`; mentions become names, links stay clickable, thread replies
show what they reply to, and Enterprise Grid orgs work (each workspace you
belong to is read separately).

**Window semantics:** these sources read a rolling window (`since_days` /
`past_days`). Items that age out of the window stay in your corpus; items that
are deleted or cancelled *inside* the window are removed at the next
reconciliation. Slack history is a skim and is never deleted. Defaults are
modest (mail 90 days, calendar 90 past/60 future, Slack 30 days): **to back-fill
your history set a larger window** — e.g. `since_days: 3650` for mail or
`since_days: 365` for Slack — and the first sync will import it all (a mailbox
of ~13,000 messages takes roughly half an hour); later runs only fetch what is
new. On Enterprise Grid, DMs and group DMs appear in every workspace; each is
read once, from a single workspace folder.

**Run only these:** `uv run workctx sync --source work-mail --source work-calendar --source work-slack`

> **Privacy:** this is *your* mailbox, diary and DMs, so the corpus will
> contain other people's private messages. Use `exclude_senders`,
> `exclude_subjects`, `exclude_channels`, `conversation_types` (drop `im` and
> `mpim`) and short windows to narrow it, keep the output folder somewhere
> private, and check your employer's information-governance policy first.
> The tool only issues read requests: nothing is sent, moved, flagged,
> marked as read, posted or reacted to.

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

### Cloud Storage Projects (OneDrive, iCloud, Dropbox)

When the project lives on cloud-synced storage, `install-service` automatically:

1. **Builds a self-contained local venv** — source code is copied (not linked), so
   the daemon runs independently of the cloud filesystem
2. **Caches the config locally** — no cloud dependency at runtime
3. **Creates `workctx-relogin`** at `~/.local/bin/` — a wrapper script for
   SharePoint re-authentication that works even when OneDrive is down
4. **Clamps output paths** to 380 characters — prevents long SharePoint paths from
   crashing OneDrive (which enforces a 400-char path limit)

If you update the source code, re-run `uv run workctx install-service` to
refresh the local install.

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
| `workctx sync --source <name>` | Sync only the named source(s); repeat the flag for several |
| `workctx status` | Shows per-source sync times and object counts |
| `workctx search "query"` | Full-text search across the entire corpus |
| `workctx daemon` | Run the daemon in the foreground (for debugging) |
| `workctx install-service` | Install background daemon (auto-starts on login) |
| `workctx remove-service` | Stop and remove the background daemon |
| `workctx service-status` | Check if the daemon is running |
| `workctx auth set <ref>` | Store a secret (token, password, etc.) |
| `workctx auth remove <ref>` | Delete a stored secret |
| `workctx auth login-sharepoint --source <name>` | Browser login for SharePoint cookie capture |
| `workctx auth login-web --source <name>` | Visible browser sign-in for an email, calendar or Slack source (rarely needed) |
| `workctx reconcile` | Force deletion detection across all sources |
| `workctx reindex` | Rebuild the full-text search index |

---

## Output Structure

```
<output_root>/
├── PROJECT_BRIEF.md              Single-file overview — upload this first
├── CHATGPT_INSTRUCTIONS.md       Ready-to-paste ChatGPT Project instructions
├── CONTEXT.md                    Corpus overview for humans & LLMs
├── AGENTS.md                     Guidance for Codex-style agents
├── CLAUDE.md                     Context for Claude Code / Claude Projects
├── README.md                     Auto-generated summary
├── _meta/
│   ├── INDEX.md                  Source overview with counts
│   ├── health.json               Sync health status
│   └── manifest.jsonl            Per-document metadata
├── confluence/<source>/<space>/<page>.md
├── jira/<source>/SUMMARY.csv              All issues in one CSV (for Gantt charts etc.)
├── jira/<source>/SUMMARY.md               Same as above, Markdown table
├── jira/<source>/<project>/<ISSUE-KEY>.md
├── sharepoint/<source>/<path>/<document>.md
├── transcripts/<source>/<year>/<date>-<meeting>-<id>.md
├── email/<source>/<year>/<month>/<date>-<subject>-<id>.md
├── calendar/<source>/<year>/<month>/<date>-<title>-<id>.md
├── slack/<source>/<workspace>/<conversation>/<YYYY-MM-DD>.md
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

## Using With AI Assistants

The corpus is designed for three usage patterns:

### ChatGPT Projects

1. Create a new ChatGPT Project
2. Copy the contents of `CHATGPT_INSTRUCTIONS.md` into the project's Custom Instructions
3. Upload `PROJECT_BRIEF.md` as a project file (gives instant overview)
4. Upload `jira/*/SUMMARY.csv` for project status / Gantt data
5. Upload specific Confluence or SharePoint files as needed

The file limit (5–40 depending on plan) means you can't upload everything.
Start with `PROJECT_BRIEF.md` and add source files as questions arise.
On paid plans, RAG handles larger uploads automatically.

### Claude Projects

1. Create a new Claude Project
2. Upload `CLAUDE.md` (or paste its contents into project instructions)
3. Upload `PROJECT_BRIEF.md` for broad context
4. Add source files as needed — Claude's RAG on paid plans handles large corpora

### Claude Code / Cursor / Codex

If your output directory is inside or adjacent to a code repository:
- `CLAUDE.md` is automatically picked up by Claude Code at session start
- `AGENTS.md` provides guidance for Codex and similar coding agents
- Use `rg` or `workctx search` to find relevant content from within the agent

### Keeping context fresh

The background daemon syncs daily. After each sync, all generated files
(`PROJECT_BRIEF.md`, `CLAUDE.md`, `SUMMARY.csv`, etc.) are regenerated
with current data. If your AI assistant doesn't auto-refresh files,
re-upload the updated versions periodically.

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
| `Config file not found` | Rename your config to `workctx.yaml`, or pass `--config path/to/config.yaml` |
| `Multiple YAML configs found` | Rename yours to `workctx.yaml` (auto-detected by convention) or use `--config` |
| `401 Unauthorized` on Confluence/Jira | Your token expired or is wrong. Generate a new one and `uv run workctx auth set <ref>` |
| `SharePoint session expired` | The automatic headless refresh already failed, so a human step is needed (password/MFA, or the ~90-day login lapsed). Run `workctx-relogin --source <name>` (works even if OneDrive is down). Falls back to `uv run workctx auth login-sharepoint --config workctx.yaml --source <name>`. Look for `Headless refresh for … found no valid cookies (last page: …)` in `logs/daemon-stderr.log` to see where SSO got stuck |
| `Sync failed with unhandled exception` + `Resource deadlock avoided` / `os error 60` while writing `_meta/*` | OneDrive File Provider hiccup. Metadata writes now retry automatically; a run-level failure is reported as FAILED (and alerted) rather than "healthy" |
| Teams transcripts sync finds nothing | Check the meeting was recorded **with transcription**, that `sharepoint_source` points at a logged-in browser-mode source, and run `uv run workctx doctor --verbose` (it probes both SharePoint hosts). Recordings in a departed colleague's locked OneDrive are skipped quietly (HTTP 423) |
| Email / calendar / Slack: `No Outlook session` / `No Slack session` | The browser profile is signed out and silent SSO could not finish (password/MFA needed). Run `uv run workctx auth login-web --source <name>`, finish signing in, then sync again. For Slack set `signin_url` to your workspace's own sign-in page (e.g. `https://my-org.enterprise.slack.com/`) so it can sign back in by itself |
| Slack shows a browser-not-supported page / nothing is found | Update Playwright's Chromium (`uv run playwright install chromium`). On Enterprise Grid make sure `client_url` is the URL you use in your browser; the org-level token cannot read messages, so the tool reads each workspace you belong to |
| `Playwright not installed` | Run `uv sync --extra playwright && uv run playwright install chromium` |
| `Lock file stale` | Another sync crashed. Delete `run.lock` from the state directory |
| `Daemon not running` | Run `uv run workctx service-status`, then `uv run workctx install-service` to reinstall |
| `Operation timed out (os error 60)` | OneDrive/cloud storage isn't serving files. Restart the OneDrive app. Use `workctx-relogin` (which bypasses OneDrive) instead of `uv run` |
| OneDrive crashes repeatedly | Long output paths (>400 chars) crash OneDrive. Run `uv run workctx install-service` to rebuild with path clamping, then sync |
| First sync is slow | Normal — it downloads everything. Check progress bars for ETA. Subsequent syncs are fast. |
| `No results` from search | Run `uv run workctx reindex` to rebuild the search index |
| Telegram spam on failures | Upgrade — the daemon now deduplicates notifications (same failure won't re-notify for 6h) |

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
  modifies, or deletes anything in Confluence, Jira, SharePoint, Teams,
  Outlook or Slack
- Email, calendar and Slack use the **session your browser already has**:
  short-lived tokens are read from the browser profile into memory for the
  run, are sent only to your own Outlook / Slack host (https, no redirects),
  and are never written to disk or logs
- A log filter prevents secrets from appearing in log files

> **Heads up:** Synchronising organisational information to a locally
> controlled directory may be subject to your employer's information
> governance policies. Check before you set this up on work content.

---

## Development

```bash
uv sync --extra dev
uv run pytest                    # 486 tests
uv run ruff check src/ tests/   # lint
uv run ruff format src/ tests/  # format
```

Contributions welcome. The architecture is documented in `docs/`.

---

## License

MIT
