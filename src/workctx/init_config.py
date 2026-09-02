"""Interactive configuration generator."""

from __future__ import annotations

from pathlib import Path

import click
import yaml
from rich.console import Console

console = Console()


def interactive_init() -> None:
    """Generate a starter YAML configuration interactively."""
    console.print("[bold]Work Context Mirror — Project Setup[/bold]")
    console.print()
    console.print(
        "[yellow]Note:[/yellow] This generates a configuration file. "
        "Secrets should be stored separately using 'workctx auth set'."
    )
    console.print()

    project_name = click.prompt("Project name", type=str)
    project_id = click.prompt(
        "Project ID (lowercase, no spaces)",
        type=str,
        default=project_name.lower().replace(" ", "-"),
    )

    import platform

    system = platform.system()
    if system in ("Darwin", "Windows"):
        default_output = str(Path.home() / "Documents" / "WorkContext" / project_id)
    else:
        default_output = str(Path.home() / "work-context" / project_id)
    output_root = click.prompt("Output directory", type=str, default=default_output)

    config: dict = {
        "version": 1,
        "project": {
            "id": project_id,
            "name": project_name,
            "output_root": output_root,
        },
        "schedule": {"hour": 5, "minute": 30},
        "sync": {
            "overlap_minutes": 15,
            "reconciliation_days": 7,
            "max_concurrency": 4,
            "large_document_chars": 300000,
        },
        "sources": {},
    }

    # Confluence
    if click.confirm("Add Confluence source?", default=False):
        conf_name = click.prompt("Confluence source name", default=f"{project_id}-wiki")
        conf_url = click.prompt("Confluence base URL (e.g. https://company.atlassian.net)")
        spaces_raw = click.prompt("Space keys (comma-separated)")
        spaces = [s.strip() for s in spaces_raw.split(",") if s.strip()]
        username = click.prompt("Atlassian username (email)")
        secret_ref = f"workctx/{project_id}/confluence"

        config["sources"]["confluence"] = [
            {
                "name": conf_name,
                "base_url": conf_url,
                "deployment": "auto",
                "spaces": spaces,
                "auth": {
                    "mode": "api_token",
                    "username": username,
                    "secret_ref": secret_ref,
                },
                "include_attachments": False,
            }
        ]
        console.print(f"  [yellow]Remember:[/yellow] workctx auth set {secret_ref}")

    # Jira
    if click.confirm("Add Jira source?", default=False):
        jira_name = click.prompt("Jira source name", default=f"{project_id}-jira")
        jira_url = click.prompt("Jira base URL (e.g. https://company.atlassian.net)")
        projects_raw = click.prompt("Project keys (comma-separated)")
        projects = [p.strip() for p in projects_raw.split(",") if p.strip()]
        username = click.prompt("Atlassian username (email)")
        secret_ref = f"workctx/{project_id}/jira"

        config["sources"]["jira"] = [
            {
                "name": jira_name,
                "base_url": jira_url,
                "deployment": "auto",
                "projects": projects,
                "auth": {
                    "mode": "api_token",
                    "username": username,
                    "secret_ref": secret_ref,
                },
                "include_comments": True,
                "include_changelog": False,
                "include_attachments": False,
            }
        ]
        console.print(f"  [yellow]Remember:[/yellow] workctx auth set {secret_ref}")

    # SharePoint
    if click.confirm("Add SharePoint source?", default=False):
        sp_name = click.prompt("SharePoint source name", default=f"{project_id}-documents")
        local_path = click.prompt("Local OneDrive path to SharePoint library")

        site_url = ""
        if click.confirm("Do you know the SharePoint site URL?", default=False):
            site_url = click.prompt("SharePoint site URL")

        config["sources"]["sharepoint"] = [
            {
                "name": sp_name,
                "site_url": site_url or None,
                "mode": "onedrive_local",
                "local_path": local_path,
                "include": ["**/*"],
                "exclude": ["**/~$*", "**/.DS_Store", "**/*.tmp"],
            }
        ]

    # Notifications
    config["notifications"] = {"macos": {"enabled": True}}
    if click.confirm("Enable Telegram notifications?", default=False):
        bot_ref = f"workctx/{project_id}/telegram-bot"
        chat_ref = f"workctx/{project_id}/telegram-chat"
        config["notifications"]["telegram"] = {
            "enabled": True,
            "bot_token_ref": bot_ref,
            "chat_id_ref": chat_ref,
        }
        console.print(f"  [yellow]Remember:[/yellow] workctx auth set {bot_ref}")
        console.print(f"  [yellow]Remember:[/yellow] workctx auth set {chat_ref}")
    else:
        config["notifications"]["telegram"] = {"enabled": False}

    # Schedule
    hour = click.prompt("Schedule hour (24h)", type=int, default=5)
    minute = click.prompt("Schedule minute", type=int, default=30)
    config["schedule"]["hour"] = hour
    config["schedule"]["minute"] = minute

    # Write config
    config_filename = f"{project_id}.yaml"
    config_path = Path.cwd() / config_filename

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    console.print()
    console.print(f"[green]Configuration written to: {config_path}[/green]")
    console.print()
    console.print("Next steps:")
    console.print("  1. Store secrets: workctx auth set <secret_ref>")
    console.print(f"  2. Validate: workctx doctor --config {config_filename}")
    console.print(f"  3. Initial sync: workctx sync --config {config_filename} --full")
    console.print(f"  4. Background daemon: workctx install-service --config {config_filename}")
