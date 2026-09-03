"""Cross-platform service management for the daemon.

Supports:
  - macOS: launchd user agent (KeepAlive, RunAtLoad)
  - Linux: systemd user service
  - Windows: Task Scheduler at-logon trigger

Also retains the legacy fixed-time schedule for backward compat.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from workctx.config import ProjectConfig

logger = logging.getLogger(__name__)

PLIST_PREFIX = "com.workctx"

_CLOUD_PATH_MARKERS = (
    "/Library/CloudStorage/",
    "/Library/Mobile Documents/",
    "/OneDrive",
    "/Dropbox/",
    "/Google Drive/",
)


def _is_cloud_synced_path(path: Path) -> bool:
    """Detect if a path is under a cloud-synced filesystem (OneDrive, iCloud, etc.)."""
    resolved = str(path.resolve())
    return any(marker in resolved for marker in _CLOUD_PATH_MARKERS)


def _local_daemon_venv_dir(config: ProjectConfig) -> Path:
    """Return a local (non-cloud) directory for the daemon's virtual environment."""
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        base = Path.home() / "AppData" / "Local"
    else:
        base = Path.home() / ".local" / "share"
    return base / "WorkContextMirror" / config.project.id / "daemon-venv"


def _build_service_command(
    config: ProjectConfig, config_path: Path, subcommand: str,
) -> tuple[list[str], dict[str, str]]:
    """Build service program arguments and extra environment variables.

    When the project lives on cloud-synced storage (OneDrive, iCloud,
    Dropbox, Google Drive), the daemon's virtual environment is
    redirected to local storage via UV_PROJECT_ENVIRONMENT to prevent
    EDEADLK (errno 11) crashes from cloud filesystem drivers.
    """
    config_abs = config_path.resolve()
    project_dir = config_abs.parent
    extra_env: dict[str, str] = {}
    tail = [subcommand, "--config", str(config_abs)]

    direct = shutil.which("workctx")
    uv = shutil.which("uv")
    on_cloud = _is_cloud_synced_path(project_dir)

    if direct and not on_cloud:
        return ([direct] + tail, extra_env)

    if uv:
        args = [uv, "run", "--project", str(project_dir), "workctx"] + tail
        if on_cloud:
            local_venv = _local_daemon_venv_dir(config)
            extra_env["UV_PROJECT_ENVIRONMENT"] = str(local_venv)
            logger.info(
                "Project on cloud storage — service venv redirected to %s",
                local_venv,
            )
        return (args, extra_env)

    if direct:
        logger.warning(
            "workctx binary is on cloud storage (%s) and 'uv' is not available. "
            "Service may fail with EDEADLK. Install 'uv' or clone project locally.",
            direct,
        )
        return ([direct] + tail, extra_env)

    raise RuntimeError("Cannot find 'workctx' or 'uv' on PATH. Install the package first.")


# ── Service install (daemon mode) ─────────────────────────────────


def install_service(config: ProjectConfig, config_path: Path) -> str:
    """Install the daemon as a background service for the current platform."""
    system = platform.system()
    if system == "Darwin":
        return _install_launchd_service(config, config_path)
    if system == "Linux":
        return _install_systemd_service(config, config_path)
    if system == "Windows":
        return _install_windows_service(config, config_path)
    raise RuntimeError(f"Unsupported platform: {system}")


def remove_service(config: ProjectConfig) -> None:
    """Remove the daemon service."""
    system = platform.system()
    if system == "Darwin":
        _remove_launchd_service(config)
    elif system == "Linux":
        _remove_systemd_service(config)
    elif system == "Windows":
        _remove_windows_service(config)


def get_service_status(config: ProjectConfig) -> dict[str, Any]:
    """Return service status for the current platform."""
    system = platform.system()
    if system == "Darwin":
        return _launchd_service_status(config)
    if system == "Linux":
        return _systemd_service_status(config)
    if system == "Windows":
        return _windows_service_status(config)
    return {"platform": system, "installed": False}


# ── macOS launchd ──────────────────────────────────────────────────


def _launchd_label(config: ProjectConfig) -> str:
    return f"{PLIST_PREFIX}.daemon.{config.project.id}"


def _launchd_plist_path(config: ProjectConfig) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_launchd_label(config)}.plist"


def _install_launchd_service(config: ProjectConfig, config_path: Path) -> str:
    import plistlib

    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    program_args, extra_env = _build_service_command(config, config_path, "daemon")
    label = _launchd_label(config)
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    env_vars = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
        + ":"
        + str(Path.home() / ".local" / "bin"),
    }
    env_vars.update(extra_env)

    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": program_args,
        "StandardOutPath": str(log_dir / "daemon-stdout.log"),
        "StandardErrorPath": str(log_dir / "daemon-stderr.log"),
        "EnvironmentVariables": env_vars,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 30,
    }

    plist_file = _launchd_plist_path(config)
    _unload_if_loaded(label, plist_file)

    with open(plist_file, "wb") as f:
        plistlib.dump(plist, f)

    _load_plist(plist_file)
    logger.info("launchd service installed: %s", plist_file)
    return str(plist_file)


def _remove_launchd_service(config: ProjectConfig) -> None:
    label = _launchd_label(config)
    plist_file = _launchd_plist_path(config)
    _unload_if_loaded(label, plist_file)
    if plist_file.exists():
        plist_file.unlink()
        logger.info("launchd service removed: %s", plist_file)


def _launchd_service_status(config: ProjectConfig) -> dict[str, Any]:
    label = _launchd_label(config)
    plist_file = _launchd_plist_path(config)
    return {
        "platform": "macOS",
        "installed": plist_file.exists(),
        "plist_path": str(plist_file),
        "label": label,
        "loaded": _is_loaded(label) if plist_file.exists() else False,
    }


# ── Linux systemd ─────────────────────────────────────────────────


def _systemd_unit_name(config: ProjectConfig) -> str:
    return f"workctx-{config.project.id}.service"


def _systemd_unit_path(config: ProjectConfig) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _systemd_unit_name(config)


def _install_systemd_service(config: ProjectConfig, config_path: Path) -> str:
    program_args, extra_env = _build_service_command(config, config_path, "daemon")
    unit_path = _systemd_unit_path(config)
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    exec_start = " ".join(program_args)
    env_lines = [f"Environment=PATH=/usr/local/bin:/usr/bin:/bin:{Path.home()}/.local/bin"]
    for k, v in extra_env.items():
        env_lines.append(f"Environment={k}={v}")

    lines = [
        "[Unit]",
        f"Description=Work Context Mirror daemon ({config.project.name})",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        "RestartSec=30",
        *env_lines,
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    unit_path.write_text("\n".join(lines) + "\n")

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", _systemd_unit_name(config)],
        capture_output=True,
    )
    logger.info("systemd user service installed: %s", unit_path)
    return str(unit_path)


def _remove_systemd_service(config: ProjectConfig) -> None:
    unit_name = _systemd_unit_name(config)
    unit_path = _systemd_unit_path(config)

    subprocess.run(
        ["systemctl", "--user", "disable", "--now", unit_name],
        capture_output=True,
    )
    if unit_path.exists():
        unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    logger.info("systemd service removed")


def _systemd_service_status(config: ProjectConfig) -> dict[str, Any]:
    unit_name = _systemd_unit_name(config)
    unit_path = _systemd_unit_path(config)
    installed = unit_path.exists()
    active = False
    if installed:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", unit_name],
            capture_output=True,
            text=True,
        )
        active = result.stdout.strip() == "active"
    return {
        "platform": "Linux",
        "installed": installed,
        "unit_path": str(unit_path),
        "active": active,
    }


# ── Windows Task Scheduler ────────────────────────────────────────


def _windows_task_name(config: ProjectConfig) -> str:
    return f"WorkContextMirror-{config.project.id}"


def _install_windows_service(config: ProjectConfig, config_path: Path) -> str:
    program_args, extra_env = _build_service_command(config, config_path, "daemon")
    task_name = _windows_task_name(config)

    exe = program_args[0]
    remaining_args = " ".join(program_args[1:])

    env_prefix = ""
    if extra_env:
        set_cmds = " && ".join(f'set "{k}={v}"' for k, v in extra_env.items())
        env_prefix = f"cmd /c {set_cmds} && "

    cmd = [
        "schtasks",
        "/create",
        "/tn",
        task_name,
        "/tr",
        f'{env_prefix}"{exe}" {remaining_args}',
        "/sc",
        "ONLOGON",
        "/rl",
        "LIMITED",
        "/f",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"schtasks failed: {result.stderr}")

    subprocess.run(
        ["schtasks", "/run", "/tn", task_name],
        capture_output=True,
    )
    logger.info("Windows scheduled task created: %s", task_name)
    return task_name


def _remove_windows_service(config: ProjectConfig) -> None:
    task_name = _windows_task_name(config)
    subprocess.run(
        ["schtasks", "/end", "/tn", task_name],
        capture_output=True,
    )
    subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True,
    )
    logger.info("Windows task removed: %s", task_name)


def _windows_service_status(config: ProjectConfig) -> dict[str, Any]:
    task_name = _windows_task_name(config)
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/fo", "LIST"],
        capture_output=True,
        text=True,
    )
    installed = result.returncode == 0
    running = "Running" in result.stdout if installed else False
    return {
        "platform": "Windows",
        "installed": installed,
        "task_name": task_name,
        "running": running,
    }


# ── Legacy schedule helpers (kept for backward compat) ─────────────


LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def _plist_label(config: ProjectConfig) -> str:
    return f"{PLIST_PREFIX}.{config.project.id}"


def _plist_path(config: ProjectConfig) -> Path:
    return LAUNCH_AGENTS_DIR / f"{_plist_label(config)}.plist"


def install_schedule(config: ProjectConfig, config_path: Path) -> Path:
    """Install a macOS launchd fixed-time schedule (legacy)."""
    import plistlib

    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    program_args, extra_env = _build_service_command(config, config_path, "sync")
    label = _plist_label(config)
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    env_vars = {"PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"}
    env_vars.update(extra_env)

    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": program_args,
        "StartCalendarInterval": {
            "Hour": config.schedule.hour,
            "Minute": config.schedule.minute,
        },
        "StandardOutPath": str(log_dir / "launchd-stdout.log"),
        "StandardErrorPath": str(log_dir / "launchd-stderr.log"),
        "EnvironmentVariables": env_vars,
        "RunAtLoad": False,
        "ProcessType": "Background",
    }

    plist_file = _plist_path(config)
    _unload_if_loaded(label, plist_file)

    with open(plist_file, "wb") as f:
        plistlib.dump(plist, f)

    _load_plist(plist_file)
    logger.info("Schedule installed: %s", plist_file)
    return plist_file


def remove_schedule(config: ProjectConfig) -> None:
    """Remove the legacy launchd schedule."""
    label = _plist_label(config)
    plist_file = _plist_path(config)
    _unload_if_loaded(label, plist_file)
    if plist_file.exists():
        plist_file.unlink()
        logger.info("Schedule removed: %s", plist_file)


def get_schedule_status(config: ProjectConfig) -> dict[str, Any]:
    """Get legacy schedule status."""
    label = _plist_label(config)
    plist_file = _plist_path(config)
    result: dict[str, Any] = {
        "installed": plist_file.exists(),
        "plist_path": str(plist_file),
        "label": label,
    }
    if plist_file.exists():
        try:
            import plistlib

            with open(plist_file, "rb") as f:
                plist = plistlib.load(f)
            cal = plist.get("StartCalendarInterval", {})
            result["time"] = f"{cal.get('Hour', '?'):02}:{cal.get('Minute', '?'):02}"
        except Exception:
            result["time"] = "unknown"
        result["loaded"] = _is_loaded(label)
    return result


# ── Shared helpers ─────────────────────────────────────────────────


def _load_plist(plist_file: Path) -> None:
    uid = os.getuid()
    try:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_file)],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["launchctl", "load", str(plist_file)],
                capture_output=True,
                timeout=10,
            )


def _unload_if_loaded(label: str, plist_file: Path) -> None:
    uid = os.getuid()
    if plist_file.exists():
        with contextlib.suppress(Exception):
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True,
                timeout=10,
            )


def _is_loaded(label: str) -> bool:
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return label in result.stdout
    except Exception:
        return False
