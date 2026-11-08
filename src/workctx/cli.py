"""CLI entry point for Work Context Mirror."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from workctx import __version__

console = Console()

DEFAULT_CONFIG_GLOB = "*.yaml"


CONFIG_ENV_VAR = "WORKCTX_CONFIG"
CONFIG_HELP = "Path to workctx.yaml (default: $WORKCTX_CONFIG, else ./workctx.yaml)"


def _find_config(config: str | None) -> Path:
    from_env = False
    if not config:
        config = os.environ.get(CONFIG_ENV_VAR) or None
        from_env = config is not None
    if config:
        p = Path(config).expanduser()
        if not p.exists():
            origin = f" (from ${CONFIG_ENV_VAR})" if from_env else ""
            console.print(f"[red]Config file not found: {p}{origin}[/red]")
            sys.exit(1)
        return p

    # Prefer the conventional name workctx.yaml / workctx.yml
    for name in ("workctx.yaml", "workctx.yml"):
        conventional = Path.cwd() / name
        if conventional.exists():
            return conventional

    candidates = list(Path.cwd().glob(DEFAULT_CONFIG_GLOB))
    yaml_configs = [c for c in candidates if c.suffix in (".yaml", ".yml")]

    # Ignore files that are clearly examples/templates
    real_configs = [
        c for c in yaml_configs if not c.stem.startswith("example") and c.stem != "example"
    ]

    if len(real_configs) == 1:
        return real_configs[0]
    if len(yaml_configs) == 1:
        return yaml_configs[0]
    if len(yaml_configs) > 1:
        names = ", ".join(c.name for c in yaml_configs)
        console.print(
            f"[red]Multiple YAML configs found ({names}). "
            f"Rename yours to workctx.yaml or specify --config.[/red]"
        )
        sys.exit(1)
    console.print(
        "[red]No config file found. Create one with 'workctx init' or specify --config.[/red]"
    )
    sys.exit(1)


_QUICK_GUIDE = """
\b
Everyday commands:
  workctx sources            what is configured, and where each source logs in
  workctx status             last successful sync per source
  workctx sync               sync now (add --source NAME for just one)
  workctx doctor             check config, logins and connectivity
  workctx auth relogin       sign back in wherever a login has lapsed
  workctx search "query"     search the mirrored corpus

\b
Which config? --config FILE, else the WORKCTX_CONFIG environment variable (set for you by
the launcher that `workctx install-service` puts in ~/.local/bin), else ./workctx.yaml.
Every command has --help.
"""


@click.group(epilog=_QUICK_GUIDE, context_settings={"max_content_width": 110})
@click.version_option(version=__version__)
def main() -> None:
    """Work Context Mirror — LLM-friendly mirror of work knowledge."""


@main.command()
@click.option("--config", "-c", type=str, default=None, help="Path to project YAML config")
@click.option("--dry-run", is_flag=True, help="Show what would change without modifying anything")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.option("--full", is_flag=True, help="Force full sync (ignore checkpoints)")
@click.option(
    "--source",
    "-s",
    "only_sources",
    multiple=True,
    help="Only sync this source (repeatable). Default: all sources.",
)
def sync(
    config: str | None,
    dry_run: bool,
    verbose: bool,
    full: bool,
    only_sources: tuple[str, ...],
) -> None:
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

    only = frozenset(only_sources) or None
    unknown = (only or frozenset()) - set(cfg.all_source_names())
    if unknown:
        console.print(f"[red]Unknown source(s): {', '.join(sorted(unknown))}[/red]")
        console.print(f"Configured sources: {', '.join(cfg.all_source_names())}")
        sys.exit(2)

    result = run_sync(cfg, run_id=run_id, dry_run=dry_run, full=full, only_sources=only)

    if dry_run:
        for sr in result.source_results:
            status_color = {
                "healthy": "green",
                "degraded": "yellow",
                "failed": "red",
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


def _source_rows(cfg: Any) -> list[tuple[str, str, str, str]]:
    """(kind, name, login, details) for every configured source. Never any secrets."""
    src = cfg.sources
    sp_by_name = {sp.name: sp for sp in src.sharepoint}
    rows: list[tuple[str, str, str, str]] = []
    for j in src.jira:
        rows.append(
            ("jira", j.name, j.auth.secret_ref or "-", f"{j.base_url}  {','.join(j.projects)}")
        )
    for c in src.confluence:
        rows.append(
            ("confluence", c.name, c.auth.secret_ref or "-", f"{c.base_url}  {','.join(c.spaces)}")
        )
    for sp in src.sharepoint:
        where = sp.site_url if sp.mode == "browser" else sp.local_path
        rows.append(("sharepoint", sp.name, f"browser profile {sp.name}", f"{sp.mode}  {where}"))
    for t in src.transcripts:
        profile = t.sharepoint_source or t.name
        rows.append(
            ("transcripts", t.name, f"browser profile {profile}", "Teams meeting transcripts")
        )
    for m in src.mail:
        rows.append(
            (
                "mail",
                m.name,
                f"browser profile {m.profile_name(sp_by_name)}",
                f"{m.mailbox_url}  last {m.since_days} days",
            )
        )
    for cal in src.calendar:
        rows.append(
            (
                "calendar",
                cal.name,
                f"browser profile {cal.profile_name(sp_by_name)}",
                f"{cal.mailbox_url}  -{cal.past_days}/+{cal.future_days} days",
            )
        )
    for sl in src.slack:
        rows.append(
            (
                "slack",
                sl.name,
                f"browser profile {sl.profile_name(sp_by_name)}",
                f"{sl.client_url}  last {sl.since_days} days",
            )
        )
    for lf in src.local_folders:
        rows.append(("local", lf.name, "-", ", ".join(lf.paths)))
    return rows


@main.command()
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
def sources(config: str | None) -> None:
    """List the configured sources, where they log in, and where the config lives."""
    from workctx.config import load_config

    config_path = _find_config(config)
    cfg = load_config(config_path)
    console.print(f"[bold]{cfg.project.name}[/bold]")
    console.print(f"Config file: {config_path}")
    console.print(f"Output:      {cfg.output_root_path}")
    console.print(f"State:       {cfg.state_dir}")
    console.print()
    table = Table(show_header=True)
    for column in ("Kind", "Name", "Login", "Details"):
        table.add_column(column, overflow="fold")
    for row in _source_rows(cfg):
        table.add_row(*row)
    console.print(table)
    console.print(
        "Re-login: [bold]workctx auth login-web --source <name>[/bold] (mail/calendar/Slack) or "
        "[bold]workctx auth login-sharepoint --source <name>[/bold]"
    )


@main.command()
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
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
                cp.last_success.strftime("%d %b %Y %H:%M") if cp and cp.last_success else "Never"
            )
            status_str = (
                "[green]Healthy[/green]" if cp and cp.last_success else "[yellow]Pending[/yellow]"
            )
            source_type = cp.source_type.value if cp else "unknown"
            table.add_row(name, source_type, f"{count:,}", last_success, status_str)

        console.print(table)
    finally:
        db.close()


@main.command()
@click.argument("query")
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
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
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
@click.option("--verbose", "-v", is_flag=True)
def doctor(config: str | None, verbose: bool) -> None:
    """Validate configuration and environment."""
    from workctx.doctor import run_doctor

    cfg_path = _find_config(config)
    run_doctor(cfg_path, verbose=verbose)


@main.command()
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
@click.option("--verbose", "-v", is_flag=True)
@click.option(
    "--force",
    is_flag=True,
    help="Allow deleting most of a source's objects (normally refused as a likely bad listing)",
)
def reconcile(config: str | None, verbose: bool, force: bool) -> None:
    """Force reconciliation of all sources (detect deletions)."""
    from workctx.config import load_config
    from workctx.logging_config import generate_run_id, setup_logging
    from workctx.sync import run_reconciliation

    cfg = load_config(_find_config(config))
    run_id = generate_run_id()
    setup_logging(cfg.state_dir, run_id, verbose=verbose)
    console.print(f"[bold]Reconciliation[/bold] — {cfg.project.name}")
    run_reconciliation(cfg, run_id=run_id, force=force)
    console.print("[green]Reconciliation complete.[/green]")


@main.command()
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
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


@main.command()
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
def verify(config: str | None) -> None:
    """Check that every indexed document still exists on disk (read-only).

    Exits 1 if files are missing. The next sync (or daemon run) re-fetches them
    automatically; nothing is changed here.
    """
    from workctx.config import load_config
    from workctx.drift import drift_report
    from workctx.state import StateDB

    cfg = load_config(_find_config(config))
    db_path = cfg.state_dir / "state.sqlite"
    if not db_path.exists():
        console.print("[yellow]No state database. Run 'workctx sync' first.[/yellow]")
        return
    db = StateDB(db_path)
    try:
        report = drift_report(db, cfg.output_root_path)
    finally:
        db.close()

    table = Table(title=f"Corpus check — {cfg.output_root_path}")
    table.add_column("Source")
    table.add_column("Indexed", justify="right")
    table.add_column("On disk", justify="right")
    table.add_column("Missing", justify="right")
    for name, (total, missing) in report.items():
        style = "red" if missing else "green"
        table.add_row(name, str(total), str(total - missing), f"[{style}]{missing}[/{style}]")
    console.print(table)

    lost = sum(missing for _, missing in report.values())
    if lost:
        console.print(
            f"[red]{lost} indexed document(s) are missing on disk.[/red] "
            "Run 'workctx sync' (or let the daemon run): they are re-fetched automatically."
        )
        sys.exit(1)
    console.print("[green]All indexed documents are present on disk.[/green]")


@main.command()
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
@click.option("--dry-run", is_flag=True, help="Show what would move without changing anything")
@click.option("--verbose", "-v", is_flag=True)
def relayout(config: str | None, dry_run: bool, verbose: bool) -> None:
    """Move an existing SharePoint corpus into the short on-disk layout (no re-download)."""
    from workctx.config import load_config
    from workctx.indexing import SearchIndex
    from workctx.locking import ExecutionLock, LockError
    from workctx.logging_config import generate_run_id, setup_logging
    from workctx.relayout import relayout_sharepoint
    from workctx.state import StateDB

    cfg = load_config(_find_config(config))
    setup_logging(cfg.state_dir, generate_run_id(), verbose=verbose)
    console.print(f"[bold]Relayout[/bold] — {cfg.project.name}")
    if dry_run:
        console.print("[yellow]DRY RUN — nothing will be moved[/yellow]")

    lock = ExecutionLock(cfg.state_dir / "run.lock")
    try:
        lock.acquire()
    except LockError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    db = StateDB(cfg.state_dir / "state.sqlite")
    idx = SearchIndex(cfg.state_dir / "state.sqlite")
    try:
        report = relayout_sharepoint(cfg, db, idx, dry_run=dry_run)
    finally:
        idx.close()
        db.close()
        lock.release()

    console.print(
        f"moved={report.moved} already_ok={report.already_ok} "
        f"re-fetch-later={report.reset} conflicts={len(report.conflicts)}"
    )
    for conflict in report.conflicts[:10]:
        console.print(f"  [yellow]conflict:[/yellow] {conflict}")
    if dry_run:
        for old, new in report.planned[:5]:
            console.print(f"  {old}\n    -> {new}")
    console.print("Next: run 'workctx sync' (regenerates the manifest and index pages).")


@main.group()
def auth() -> None:
    """Sign in (relogin) and manage stored secrets.

    \b
    workctx auth relogin         sign back in wherever a login has lapsed
    workctx auth set REF         store a token/password in the OS credential store
    workctx auth login-sharepoint / login-web   the individual sign-in flows
    """


@auth.command("set")
@click.argument("secret_ref")
@click.option("--value", prompt=True, hide_input=True, confirmation_prompt=True)
def auth_set(secret_ref: str, value: str) -> None:
    """Store a secret in the OS credential store."""
    from workctx.secrets import set_secret

    set_secret(secret_ref, value)
    console.print(f"[green]Secret stored: {secret_ref}[/green]")


@auth.command("remove")
@click.argument("secret_ref")
def auth_remove(secret_ref: str) -> None:
    """Remove a secret from the OS credential store."""
    from workctx.secrets import delete_secret

    delete_secret(secret_ref)
    console.print(f"[green]Secret removed: {secret_ref}[/green]")


@auth.command("login-web")
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
@click.option(
    "--source",
    "-s",
    type=str,
    default=None,
    help="Name of one mail, calendar or slack source (default: every web source in the config)",
)
@click.option(
    "--force", is_flag=True, help="Open the sign-in window even if the session is still valid"
)
def auth_login_web(config: str | None, source: str | None, force: bool) -> None:
    """Sign in to Outlook / Slack in a visible browser (saved in the browser profile).

    Each selected source is first checked silently; a window opens only for sessions that
    have actually lapsed (use --force to open regardless). Without --source every
    mail, calendar and Slack source is checked; sources sharing a browser profile and URL
    are handled once.
    """
    from workctx.auth.webtokens import check_web_session, interactive_web_login
    from workctx.config import load_config

    cfg = load_config(_find_config(config))
    sp_by_name = {sp.name: sp for sp in cfg.sources.sharepoint}
    targets: list[tuple[str, str, str, Any, str]] = []  # (kind, profile, url, src, name)
    seen: set[tuple[str, str, str]] = set()
    for kind, group in (
        ("outlook", cfg.sources.mail),
        ("outlook", cfg.sources.calendar),
        ("slack", cfg.sources.slack),
    ):
        for src in group:
            if source is not None and src.name != source:
                continue
            profile = src.profile_name(sp_by_name)
            url = src.client_url if kind == "slack" else src.mailbox_url  # type: ignore[union-attr]
            if (kind, profile, url) in seen:
                continue
            seen.add((kind, profile, url))
            targets.append((kind, profile, url, src, src.name))
    if not targets:
        what = f"named '{source}'" if source else "configured"
        console.print(f"[red]No mail/calendar/slack source {what} in the config.[/red]")
        sys.exit(1)
    failed = False
    for kind, profile, url, src, name in targets:
        extra: dict[str, Any] = {"sso_button_text": getattr(src, "sso_button_text", "Sign in with")}
        if kind == "outlook":
            extra["api_base"] = src.api_base
        else:
            extra["signin_url"] = getattr(src, "signin_url", None)
        if not force:
            console.print(f"Checking '{name}' (profile [bold]{profile}[/bold]) ...")
            if check_web_session(profile, url, kind, **extra):
                console.print(f"[green]'{name}' is already signed in; nothing to do.[/green]")
                continue
            console.print(f"[yellow]'{name}' has lapsed.[/yellow]")
        console.print(f"Opening a browser (profile [bold]{profile}[/bold]) at {url} ...")
        console.print("Complete sign-in in the window; it closes automatically.")
        if interactive_web_login(profile, url, kind, **extra):
            console.print(f"[green]Signed in. '{name}' can now sync.[/green]")
        else:
            console.print(f"[red]Timed out before a usable session was detected ({name}).[/red]")
            failed = True
    if failed:
        sys.exit(1)


@auth.command("login-sharepoint")
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
@click.option(
    "--source",
    "-s",
    type=str,
    required=True,
    help="SharePoint source name (or a standalone Teams transcripts source)",
)
@click.option("--headless", is_flag=True, help="Run headless (for testing)")
def auth_login_sharepoint(config: str | None, source: str, headless: bool) -> None:
    """Open browser for SharePoint login and capture session cookies.

    Prefer workctx auth relogin: it checks first and covers every browser login.
    """
    from workctx.auth.sharepoint import interactive_login
    from workctx.config import load_config

    cfg = load_config(_find_config(config))
    site_url: str | None = None
    secret_ref: str | None = None

    for sp in cfg.sources.sharepoint:
        if sp.name == source:
            site_url = sp.site_url
            secret_ref = sp.auth.secret_ref if sp.auth else None
            break
    else:
        for tx in cfg.sources.transcripts:
            if tx.name != source:
                continue
            if tx.sharepoint_source:
                console.print(
                    f"[yellow]'{source}' reuses the login of SharePoint source "
                    f"'{tx.sharepoint_source}'.[/yellow]\n"
                    f"Run: workctx auth login-sharepoint --source {tx.sharepoint_source}"
                )
                sys.exit(1)
            site_url = tx.site_url
            secret_ref = tx.auth.secret_ref if tx.auth else None
            break
        else:
            web_names = {
                s.name
                for group in (cfg.sources.mail, cfg.sources.calendar, cfg.sources.slack)
                for s in group
            }
            if source in web_names:
                # Email, calendar and Slack sign in through the same browser profile but
                # need their own flow; don't make the user hunt for the right command.
                console.print(
                    f"[yellow]'{source}' is not a SharePoint source; using the web sign-in "
                    f"(workctx auth login-web --source {source}).[/yellow]"
                )
                click.get_current_context().invoke(
                    auth_login_web, config=config, source=source, force=False
                )
                return
            console.print(f"[red]SharePoint source '{source}' not found in config.[/red]")
            console.print("Run 'workctx sources' to list the configured sources.")
            sys.exit(1)

    if not site_url:
        console.print(f"[red]site_url not configured for '{source}'.[/red]")
        sys.exit(1)
    if not secret_ref:
        console.print(f"[red]auth.secret_ref not configured for '{source}'.[/red]")
        sys.exit(1)

    console.print(f"Opening browser for [bold]{site_url}[/bold]...")
    console.print("Complete authentication in the browser window.")

    try:
        cookies = interactive_login(
            site_url,
            source,
            secret_ref,
            headless=headless,
        )
        console.print(f"[green]Cookies captured: {', '.join(cookies.keys())}[/green]")
    except Exception as e:
        console.print(f"[red]Login failed: {e}[/red]")
        sys.exit(1)


def _sharepoint_login_ok(site_url: str | None, profile: str, secret_ref: str | None) -> bool:
    """Silent check: does a session SharePoint accepts exist (or renew by itself)?"""
    from workctx.auth.sharepoint import get_valid_cookies

    if not (site_url and secret_ref):
        return False
    try:
        get_valid_cookies(site_url, profile, secret_ref)
    except Exception as exc:  # session expired, no profile, no network ...
        console.print(f"  [dim]{type(exc).__name__}: {exc}[/dim]")
        return False
    return True


@auth.command("relogin")
@click.argument("name", required=False)
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
@click.option("--source", "-s", type=str, default=None, help="Source name (same as NAME)")
@click.option("--force", is_flag=True, help="Open the sign-in window even if still signed in")
def auth_relogin(name: str | None, config: str | None, source: str | None, force: bool) -> None:
    """Sign back in wherever a login has lapsed.

    Works for every browser-login source: SharePoint, Teams transcripts, email, calendar
    and Slack. Each is checked silently first and a window opens only for logins that have
    really lapsed (--force opens regardless). Give a source NAME (see
    workctx sources) to handle just that one.
    """
    from workctx.config import load_config

    wanted = source or name
    cfg = load_config(_find_config(config))
    src = cfg.sources
    sp_sources = [s for s in src.sharepoint if s.mode == "browser"]
    own_login_transcripts = [t for t in src.transcripts if not t.sharepoint_source]
    web_names = {s.name for group in (src.mail, src.calendar, src.slack) for s in group}
    # A transcripts source that borrows a SharePoint login is covered by that SharePoint source.
    borrowed = {t.name: t.sharepoint_source for t in src.transcripts if t.sharepoint_source}
    if wanted in borrowed:
        console.print(f"'{wanted}' uses the login of SharePoint source '{borrowed[wanted]}'.")
        wanted = str(borrowed[wanted])
    known = [s.name for s in sp_sources] + [t.name for t in own_login_transcripts]
    known += sorted(web_names) + list(borrowed)
    if wanted is not None and wanted not in known:
        console.print(f"[red]No source named '{wanted}' with a browser login.[/red]")
        console.print("Sources you can sign in to: " + (", ".join(known) or "(none)"))
        console.print("See them all with: workctx sources")
        sys.exit(1)

    ctx = click.get_current_context()
    failed = False
    sp_targets: list[tuple[str, str | None, str | None]] = [
        (s.name, s.site_url, s.auth.secret_ref if s.auth else None) for s in sp_sources
    ] + [(t.name, t.site_url, t.auth.secret_ref if t.auth else None) for t in own_login_transcripts]
    for sp_name, site_url, secret_ref in sp_targets:
        if wanted is not None and wanted != sp_name:
            continue
        if not force:
            console.print(f"Checking '{sp_name}' ...")
            if _sharepoint_login_ok(site_url, sp_name, secret_ref):
                console.print(f"[green]'{sp_name}' is already signed in; nothing to do.[/green]")
                continue
            console.print(f"[yellow]'{sp_name}' has lapsed.[/yellow]")
        try:
            ctx.invoke(auth_login_sharepoint, config=config, source=sp_name, headless=False)
        except SystemExit as exc:
            failed = failed or exc.code not in (0, None)
    if wanted is None or wanted in web_names:
        try:
            ctx.invoke(
                auth_login_web,
                config=config,
                source=wanted if wanted in web_names else None,
                force=force,
            )
        except SystemExit as exc:
            failed = failed or exc.code not in (0, None)
    if failed:
        sys.exit(1)


@main.command("install-schedule")
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
def install_schedule(config: str | None) -> None:
    """Install a launchd schedule for automatic sync."""
    from workctx.config import load_config
    from workctx.scheduler import install_schedule

    cfg = load_config(_find_config(config))
    config_path = _find_config(config)
    plist_path = install_schedule(cfg, config_path)
    console.print(f"[green]Schedule installed: {plist_path}[/green]")
    console.print(f"Sync will run daily at {cfg.schedule.hour:02d}:{cfg.schedule.minute:02d}")


@main.command("remove-schedule")
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
def remove_schedule(config: str | None) -> None:
    """Remove the launchd schedule."""
    from workctx.config import load_config
    from workctx.scheduler import remove_schedule

    cfg = load_config(_find_config(config))
    remove_schedule(cfg)
    console.print("[green]Schedule removed.[/green]")


@main.command("schedule-status")
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
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
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
def daemon(config: str | None) -> None:
    """Run as a background daemon (daily sync + Telegram commands)."""
    from workctx.daemon import run_daemon

    config_path = str(_find_config(config))
    console.print("[bold]Work Context Mirror — Daemon Mode[/bold]")
    console.print("Press Ctrl+C to stop.\n")
    run_daemon(config_path)


@main.command("install-service")
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
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
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
def remove_service_cmd(config: str | None) -> None:
    """Remove the background daemon service."""
    from workctx.config import load_config
    from workctx.scheduler import remove_service

    cfg = load_config(_find_config(config))
    remove_service(cfg)
    console.print("[green]Service removed.[/green]")


@main.command("service-status")
@click.option("--config", "-c", type=str, default=None, help=CONFIG_HELP)
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
        console.print("Run: uv run workctx install-service")
