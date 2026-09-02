"""CLI entry point for Work Context Mirror."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from workctx import __version__

console = Console()

DEFAULT_CONFIG_GLOB = "*.yaml"


def _find_config(config: str | None) -> Path:
    if config:
        p = Path(config)
        if not p.exists():
            console.print(f"[red]Config file not found: {p}[/red]")
            sys.exit(1)
        return p

    candidates = list(Path.cwd().glob(DEFAULT_CONFIG_GLOB))
    yaml_configs = [c for c in candidates if c.suffix in (".yaml", ".yml")]
    if len(yaml_configs) == 1:
        return yaml_configs[0]
    if len(yaml_configs) > 1:
        console.print("[red]Multiple YAML configs found. Specify with --config.[/red]")
        sys.exit(1)
    console.print(
        "[red]No config file found. Create one with 'workctx init' "
        "or specify --config.[/red]"
    )
    sys.exit(1)


@click.group()
@click.version_option(version=__version__)
def main() -> None:
    """Work Context Mirror — LLM-friendly mirror of work knowledge."""


@main.command()
@click.option("--config", "-c", type=str, default=None, help="Path to project YAML config")
@click.option("--dry-run", is_flag=True, help="Show what would change without modifying anything")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.option("--full", is_flag=True, help="Force full sync (ignore checkpoints)")
def sync(config: str | None, dry_run: bool, verbose: bool, full: bool) -> None:
    """Run incremental synchronisation."""
    from workctx.config import load_config
    from workctx.logging_config import generate_run_id, setup_logging
    from workctx.sync import run_sync

    cfg = load_config(_find_config(config))
    run_id = generate_run_id()
    log_file = setup_logging(cfg.state_dir, run_id, verbose=verbose)

    console.print(f"[bold]Work Context Mirror[/bold] — {cfg.project.name}")
    console.print(f"Run: {run_id}")
    if dry_run:
        console.print("[yellow]DRY RUN — no changes will be made[/yellow]")
    console.print()

    result = run_sync(cfg, run_id=run_id, dry_run=dry_run, full=full)

    if dry_run:
        for sr in result.source_results:
            status_color = {
                "healthy": "green", "degraded": "yellow", "failed": "red",
            }[sr.status.value]
            console.print(
                f"  [{status_color}]{sr.source_type.value}/{sr.source_name}: "
                f"{sr.status.value.upper()}[/{status_color}] — "
                f"checked={sr.objects_checked} added={sr.objects_added} "
                f"updated={sr.objects_updated} deleted={sr.objects_deleted} "
                f"failed={sr.objects_failed}"
            )

    overall = result.aggregate_status()
    status_color = {"healthy": "green", "degraded": "yellow", "failed": "red"}[overall.value]
    console.print(f"\nOverall: [{status_color}]{overall.value.upper()}[/{status_color}]")
    console.print(f"Log: {log_file}")

    if overall.value == "failed":
        sys.exit(1)


@main.command()
@click.option("--config", "-c", type=str, default=None)
def status(config: str | None) -> None:
    """Display project sync status."""
    from workctx.config import load_config
    from workctx.state import StateDB

    cfg = load_config(_find_config(config))
    db_path = cfg.state_dir / "state.sqlite"
    if not db_path.exists():
        console.print("[yellow]No state database found. Run 'workctx sync' first.[/yellow]")
        return

    db = StateDB(db_path)
    try:
        console.print(f"[bold]{cfg.project.name}[/bold]")
        console.print()

        table = Table(show_header=True)
        table.add_column("Source")
        table.add_column("Type")
        table.add_column("Objects")
        table.add_column("Last Success")
        table.add_column("Status")

        for name in cfg.all_source_names():
            cp = db.get_checkpoint(name)
            count = db.count_objects(name)
            last_success = (
                cp.last_success.strftime("%d %b %Y %H:%M")
                if cp and cp.last_success
                else "Never"
            )
            status_str = (
                "[green]Healthy[/green]"
                if cp and cp.last_success
                else "[yellow]Pending[/yellow]"
            )
            source_type = cp.source_type.value if cp else "unknown"
            table.add_row(name, source_type, f"{count:,}", last_success, status_str)

        console.print(table)
    finally:
        db.close()


@main.command()
@click.argument("query")
@click.option("--config", "-c", type=str, default=None)
@click.option("--limit", "-n", type=int, default=10)
def search(query: str, config: str | None, limit: int) -> None:
    """Search the normalised corpus."""
    from workctx.config import load_config
    from workctx.indexing import SearchIndex

    cfg = load_config(_find_config(config))
    db_path = cfg.state_dir / "state.sqlite"
    if not db_path.exists():
        console.print("[yellow]No index found. Run 'workctx sync' first.[/yellow]")
        return

    idx = SearchIndex(db_path)
    try:
        results = idx.search(query, limit=limit)
        if not results:
            console.print("[yellow]No results found.[/yellow]")
            return
        for i, r in enumerate(results, 1):
            console.print(f"[bold]{i}. {r['output_path']}[/bold]")
            if r.get("title"):
                console.print(f"   {r['title']}")
            if r.get("snippet"):
                console.print(f"   ...{r['snippet']}...")
            console.print()
    finally:
        idx.close()


@main.command()
@click.option("--config", "-c", type=str, default=None)
@click.option("--verbose", "-v", is_flag=True)
def doctor(config: str | None, verbose: bool) -> None:
    """Validate configuration and environment."""
    from workctx.doctor import run_doctor

    cfg_path = _find_config(config)
    run_doctor(cfg_path, verbose=verbose)


@main.command()
@click.option("--config", "-c", type=str, default=None)
@click.option("--verbose", "-v", is_flag=True)
def reconcile(config: str | None, verbose: bool) -> None:
    """Force reconciliation of all sources (detect deletions)."""
    from workctx.config import load_config
    from workctx.logging_config import generate_run_id, setup_logging
    from workctx.sync import run_reconciliation

    cfg = load_config(_find_config(config))
    run_id = generate_run_id()
    setup_logging(cfg.state_dir, run_id, verbose=verbose)
    console.print(f"[bold]Reconciliation[/bold] — {cfg.project.name}")
    run_reconciliation(cfg, run_id=run_id)
    console.print("[green]Reconciliation complete.[/green]")


@main.command()
@click.option("--config", "-c", type=str, default=None)
def reindex(config: str | None) -> None:
    """Rebuild the full-text search index."""
    from workctx.config import load_config
    from workctx.indexing import SearchIndex

    cfg = load_config(_find_config(config))
    db_path = cfg.state_dir / "state.sqlite"
    if not db_path.exists():
        console.print("[yellow]No state database. Run 'workctx sync' first.[/yellow]")
        return
    idx = SearchIndex(db_path)
    try:
        count = idx.rebuild_from_corpus(cfg.output_root_path)
        console.print(f"[green]Indexed {count} documents.[/green]")
    finally:
        idx.close()


@main.group()
def auth() -> None:
    """Manage authentication secrets."""


@auth.command("set")
@click.argument("secret_ref")
@click.option("--value", prompt=True, hide_input=True, confirmation_prompt=True)
def auth_set(secret_ref: str, value: str) -> None:
    """Store a secret in macOS Keychain."""
    from workctx.secrets import set_secret

    set_secret(secret_ref, value)
    console.print(f"[green]Secret stored: {secret_ref}[/green]")


@auth.command("remove")
@click.argument("secret_ref")
def auth_remove(secret_ref: str) -> None:
    """Remove a secret from macOS Keychain."""
    from workctx.secrets import delete_secret

    delete_secret(secret_ref)
    console.print(f"[green]Secret removed: {secret_ref}[/green]")


@auth.command("login-sharepoint")
@click.option("--config", "-c", type=str, default=None)
@click.option("--source", "-s", type=str, required=True, help="SharePoint source name")
@click.option("--headless", is_flag=True, help="Run headless (for testing)")
def auth_login_sharepoint(config: str | None, source: str, headless: bool) -> None:
    """Open browser for SharePoint login and capture session cookies."""
    from workctx.auth.sharepoint import interactive_login
    from workctx.config import load_config

    cfg = load_config(_find_config(config))
    sp_config = None
    for sp in cfg.sources.sharepoint:
        if sp.name == source:
            sp_config = sp
            break

    if not sp_config:
        console.print(f"[red]SharePoint source '{source}' not found in config.[/red]")
        sys.exit(1)
    if not sp_config.site_url:
        console.print(f"[red]site_url not configured for '{source}'.[/red]")
        sys.exit(1)
    if not sp_config.auth or not sp_config.auth.secret_ref:
        console.print(f"[red]auth.secret_ref not configured for '{source}'.[/red]")
        sys.exit(1)

    console.print(f"Opening browser for [bold]{sp_config.site_url}[/bold]...")
    console.print("Complete authentication in the browser window.")

    try:
        cookies = interactive_login(
            sp_config.site_url,
            source,
            sp_config.auth.secret_ref,
            headless=headless,
        )
        console.print(
            f"[green]Cookies captured: {', '.join(cookies.keys())}[/green]"
        )
    except Exception as e:
        console.print(f"[red]Login failed: {e}[/red]")
        sys.exit(1)


@main.command("install-schedule")
@click.option("--config", "-c", type=str, default=None)
def install_schedule(config: str | None) -> None:
    """Install a launchd schedule for automatic sync."""
    from workctx.config import load_config
    from workctx.scheduler import install_schedule

    cfg = load_config(_find_config(config))
    config_path = _find_config(config)
    plist_path = install_schedule(cfg, config_path)
    console.print(f"[green]Schedule installed: {plist_path}[/green]")
    console.print(
        f"Sync will run daily at {cfg.schedule.hour:02d}:{cfg.schedule.minute:02d}"
    )


@main.command("remove-schedule")
@click.option("--config", "-c", type=str, default=None)
def remove_schedule(config: str | None) -> None:
    """Remove the launchd schedule."""
    from workctx.config import load_config
    from workctx.scheduler import remove_schedule

    cfg = load_config(_find_config(config))
    remove_schedule(cfg)
    console.print("[green]Schedule removed.[/green]")


@main.command("schedule-status")
@click.option("--config", "-c", type=str, default=None)
def schedule_status(config: str | None) -> None:
    """Show scheduler status."""
    from workctx.config import load_config
    from workctx.scheduler import get_schedule_status

    cfg = load_config(_find_config(config))
    status_info = get_schedule_status(cfg)
    if status_info["installed"]:
        console.print(f"[green]Schedule installed[/green]: {status_info['plist_path']}")
        console.print(f"  Time: {status_info.get('time', 'unknown')}")
        console.print(f"  Loaded: {status_info.get('loaded', 'unknown')}")
    else:
        console.print("[yellow]No schedule installed.[/yellow]")


@main.command()
def init() -> None:
    """Interactively generate a starter configuration."""
    from workctx.init_config import interactive_init

    interactive_init()


@main.command()
@click.option("--config", "-c", type=str, default=None)
def daemon(config: str | None) -> None:
    """Run as a background daemon (daily sync + Telegram commands)."""
    from workctx.daemon import run_daemon

    config_path = str(_find_config(config))
    console.print("[bold]Work Context Mirror — Daemon Mode[/bold]")
    console.print("Press Ctrl+C to stop.\n")
    run_daemon(config_path)


@main.command("install-service")
@click.option("--config", "-c", type=str, default=None)
def install_service_cmd(config: str | None) -> None:
    """Install the daemon as a background service (auto-start on login)."""
    from workctx.config import load_config
    from workctx.scheduler import install_service

    cfg = load_config(_find_config(config))
    config_path = _find_config(config)
    result = install_service(cfg, config_path)
    console.print(f"[green]Service installed: {result}[/green]")
    console.print("The daemon will start automatically on login and sync daily.")
    console.print("Telegram commands: /sync, /status, /help")


@main.command("remove-service")
@click.option("--config", "-c", type=str, default=None)
def remove_service_cmd(config: str | None) -> None:
    """Remove the background daemon service."""
    from workctx.config import load_config
    from workctx.scheduler import remove_service

    cfg = load_config(_find_config(config))
    remove_service(cfg)
    console.print("[green]Service removed.[/green]")


@main.command("service-status")
@click.option("--config", "-c", type=str, default=None)
def service_status_cmd(config: str | None) -> None:
    """Show background daemon service status."""
    from workctx.config import load_config
    from workctx.scheduler import get_service_status

    cfg = load_config(_find_config(config))
    info = get_service_status(cfg)
    if info.get("installed"):
        console.print(f"[green]Service installed[/green] ({info.get('platform', '?')})")
        for k, v in info.items():
            if k not in ("installed", "platform"):
                console.print(f"  {k}: {v}")
    else:
        console.print("[yellow]No service installed.[/yellow]")
        console.print("Run: workctx install-service")
