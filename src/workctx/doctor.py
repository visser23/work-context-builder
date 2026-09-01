"""Configuration and environment validation."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from rich.console import Console

from workctx.config import load_config
from workctx.secrets import get_secret

logger = logging.getLogger(__name__)
console = Console()


def run_doctor(config_path: Path, *, verbose: bool = False) -> bool:
    """Validate configuration and environment. Returns True if all checks pass."""
    checks_passed = 0
    checks_failed = 0
    checks_warned = 0

    def ok(msg: str) -> None:
        nonlocal checks_passed
        console.print(f"  [green]✓[/green] {msg}")
        checks_passed += 1

    def fail(msg: str) -> None:
        nonlocal checks_failed
        console.print(f"  [red]✗[/red] {msg}")
        checks_failed += 1

    def warn(msg: str) -> None:
        nonlocal checks_warned
        console.print(f"  [yellow]![/yellow] {msg}")
        checks_warned += 1

    console.print("[bold]Work Context Mirror — Doctor[/bold]")
    console.print()

    # 1. Config file
    console.print("[bold]Configuration[/bold]")
    try:
        config = load_config(config_path)
        ok(f"Config loaded: {config_path}")
    except Exception as e:
        fail(f"Config load failed: {e}")
        console.print(f"\n[red]{checks_failed} failed[/red]")
        return False

    # 2. Project info
    console.print()
    console.print("[bold]Project[/bold]")
    ok(f"ID: {config.project.id}")
    ok(f"Name: {config.project.name}")

    # 3. Output directory
    console.print()
    console.print("[bold]Output Directory[/bold]")
    output = config.output_root_path
    if output.exists():
        ok(f"Exists: {output}")
        if output.is_dir():
            ok("Is a directory")
        else:
            fail("Not a directory")
    else:
        warn(f"Does not exist (will be created): {output}")

    # 4. State directory
    console.print()
    console.print("[bold]State Directory[/bold]")
    state_dir = config.state_dir
    if state_dir.exists():
        ok(f"Exists: {state_dir}")
    else:
        warn(f"Does not exist (will be created): {state_dir}")

    # 5. SQLite + FTS5
    console.print()
    console.print("[bold]SQLite[/bold]")
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE test_fts USING fts5(content)")
        conn.close()
        ok("SQLite FTS5 available")
    except Exception as e:
        fail(f"SQLite FTS5 not available: {e}")

    # 6. Document converters
    console.print()
    console.print("[bold]Document Converters[/bold]")
    try:
        from markitdown import MarkItDown  # noqa: F401
        ok("MarkItDown available")
    except ImportError:
        fail("MarkItDown not installed (pip install markitdown)")

    try:
        import pymupdf4llm  # noqa: F401
        ok("PyMuPDF4LLM available")
    except ImportError:
        warn("PyMuPDF4LLM not installed (pip install pymupdf4llm) — PDF conversion unavailable")

    try:
        from docling.document_converter import DocumentConverter  # noqa: F401
        ok("Docling available (optional fallback)")
    except ImportError:
        if verbose:
            warn("Docling not installed (optional)")

    # 7. Sources
    console.print()
    console.print("[bold]Sources[/bold]")

    for sp in config.sources.sharepoint:
        console.print(f"\n  SharePoint: {sp.name}")
        if sp.mode == "onedrive_local":
            if sp.local_path:
                local = Path(sp.local_path)
                if local.exists() and local.is_dir():
                    file_count = sum(1 for _ in local.rglob("*") if _.is_file())
                    ok(f"Local path exists: {local} ({file_count} files)")
                elif local.exists():
                    fail(f"Local path is not a directory: {local}")
                else:
                    fail(f"Local path does not exist: {local}")
            else:
                fail("local_path not configured")
        else:
            warn(f"Mode '{sp.mode}' — advanced configuration")

    for jira in config.sources.jira:
        console.print(f"\n  Jira: {jira.name}")
        ok(f"Base URL: {jira.base_url}")
        ok(f"Projects: {', '.join(jira.projects)}")

        if jira.auth.secret_ref:
            secret = get_secret(jira.auth.secret_ref)
            if secret:
                ok(f"API token found for {jira.auth.secret_ref}")
                _check_jira_connection(jira, secret, ok, fail, warn)
            else:
                fail(
                    f"No API token for {jira.auth.secret_ref}. "
                    f"Run: workctx auth set {jira.auth.secret_ref}"
                )

    for conf in config.sources.confluence:
        console.print(f"\n  Confluence: {conf.name}")
        ok(f"Base URL: {conf.base_url}")
        ok(f"Spaces: {', '.join(conf.spaces)}")

        if conf.auth.secret_ref:
            secret = get_secret(conf.auth.secret_ref)
            if secret:
                ok(f"API token found for {conf.auth.secret_ref}")
                _check_confluence_connection(conf, secret, ok, fail, warn)
            else:
                fail(
                    f"No API token for {conf.auth.secret_ref}. "
                    f"Run: workctx auth set {conf.auth.secret_ref}"
                )

    # 8. Notifications
    console.print()
    console.print("[bold]Notifications[/bold]")
    if config.notifications.telegram.enabled:
        tg = config.notifications.telegram
        bot_token = get_secret(tg.bot_token_ref or "") if tg.bot_token_ref else None
        chat_id = get_secret(tg.chat_id_ref or "") if tg.chat_id_ref else None
        if bot_token and chat_id:
            ok("Telegram configured")
        else:
            if not bot_token:
                fail(f"Telegram bot token missing: {tg.bot_token_ref}")
            if not chat_id:
                fail(f"Telegram chat ID missing: {tg.chat_id_ref}")
    else:
        warn("Telegram notifications disabled")

    if config.notifications.macos.enabled:
        ok("macOS notifications enabled")

    # 9. Scheduler
    console.print()
    console.print("[bold]Scheduler[/bold]")
    from workctx.scheduler import get_schedule_status
    sched = get_schedule_status(config)
    if sched["installed"]:
        ok(f"Schedule installed: {sched.get('time', 'unknown')}")
        if sched.get("loaded"):
            ok("Schedule loaded in launchd")
        else:
            warn("Schedule file exists but may not be loaded")
    else:
        warn("No schedule installed. Run: workctx install-schedule")

    # Summary
    console.print()
    console.print("[bold]Summary[/bold]")
    if checks_failed == 0:
        console.print(f"  [green]{checks_passed} passed[/green], {checks_warned} warnings")
    else:
        console.print(
            f"  [green]{checks_passed} passed[/green], "
            f"[red]{checks_failed} failed[/red], "
            f"{checks_warned} warnings"
        )

    return checks_failed == 0


def _check_jira_connection(jira_config, secret: str, ok, fail, warn) -> None:
    """Test Jira API connectivity (Cloud v3 + DC v2 with PAT)."""
    import httpx

    base = jira_config.base_url.rstrip("/")

    for api_ver, auth_style in [("2", "bearer"), ("3", "basic"), ("2", "basic")]:
        try:
            if auth_style == "bearer":
                resp = httpx.get(
                    f"{base}/rest/api/{api_ver}/myself",
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Accept": "application/json",
                    },
                    timeout=15.0,
                )
            else:
                resp = httpx.get(
                    f"{base}/rest/api/{api_ver}/myself",
                    auth=httpx.BasicAuth(
                        jira_config.auth.username or "", secret
                    ),
                    timeout=15.0,
                    headers={"Accept": "application/json"},
                )
            if resp.status_code == 200:
                user = resp.json()
                mode = "DC/PAT" if auth_style == "bearer" else "Cloud"
                ok(
                    f"Jira connected as: "
                    f"{user.get('displayName', 'unknown')} ({mode})"
                )
                return
        except Exception:
            continue

    fail("Jira authentication failed — check token and base URL")


def _check_confluence_connection(
    conf_config, secret: str, ok, fail, warn
) -> None:
    """Test Confluence API connectivity (Cloud + DC with PAT)."""
    import httpx

    base = conf_config.base_url.rstrip("/")

    for api_base, auth_style in [
        (f"{base}/rest/api", "bearer"),
        (f"{base}/wiki/rest/api", "bearer"),
        (f"{base}/wiki/rest/api", "basic"),
    ]:
        try:
            if auth_style == "bearer":
                resp = httpx.get(
                    f"{api_base}/space",
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Accept": "application/json",
                    },
                    params={"limit": 1},
                    timeout=15.0,
                )
            else:
                resp = httpx.get(
                    f"{api_base}/space",
                    auth=httpx.BasicAuth(
                        conf_config.auth.username or "", secret
                    ),
                    timeout=15.0,
                    headers={"Accept": "application/json"},
                    params={"limit": 1},
                )
            if resp.status_code == 200:
                mode = "DC/PAT" if auth_style == "bearer" else "Cloud"
                ok(f"Confluence connected ({mode} via {api_base})")
                return
        except Exception:
            continue

    fail("Confluence authentication failed — check token and base URL")
