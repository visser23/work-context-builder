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
import platform
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from workctx.config import ProjectConfig

logger = logging.getLogger(__name__)

PLIST_PREFIX = "com.workctx"


def _resolve_workctx_bin() -> str:
    """Find the workctx binary or fall back to uv run workctx."""
    direct = shutil.which("workctx")
    if direct:
        return direct
    uv = shutil.which("uv")
    if uv:
        return f"{uv} run workctx"
    raise RuntimeError(
        "Cannot find 'workctx' on PATH. Install the package first."
    )


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
    return (
        Path.home()
        / "Library"
        / "LaunchAgents"
        / f"{_launchd_label(config)}.plist"
    )


def _install_launchd_service(config: ProjectConfig, config_path: Path) -> str:
    import plistlib

    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    workctx_bin = _resolve_workctx_bin()
    label = _launchd_label(config)
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if " " in workctx_bin:
        parts = workctx_bin.split(" ", 1)
        program_args = [
            parts[0], parts[1], "daemon",
            "--config", str(config_path.resolve()),
        ]
    else:
        program_args = [
            workctx_bin, "daemon",
            "--config", str(config_path.resolve()),
        ]

    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": program_args,
        "StandardOutPath": str(log_dir / "daemon-stdout.log"),
        "StandardErrorPath": str(log_dir / "daemon-stderr.log"),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
                    + ":" + str(Path.home() / ".local" / "bin"),
        },
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
    return (
        Path.home()
        / ".config"
        / "systemd"
        / "user"
        / _systemd_unit_name(config)
    )


def _install_systemd_service(config: ProjectConfig, config_path: Path) -> str:
    workctx_bin = _resolve_workctx_bin()
    unit_path = _systemd_unit_path(config)
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    if " " in workctx_bin:
        exec_start = f"{workctx_bin} daemon --config {config_path.resolve()}"
    else:
        exec_start = f"{workctx_bin} daemon --config {config_path.resolve()}"

    unit = textwrap.dedent(f"""\
        [Unit]
        Description=Work Context Mirror daemon ({config.project.name})
        After=network-online.target

        [Service]
        Type=simple
        ExecStart={exec_start}
        Restart=on-failure
        RestartSec=30
        Environment=PATH=/usr/local/bin:/usr/bin:/bin:{Path.home()}/.local/bin

        [Install]
        WantedBy=default.target
    """)

    unit_path.write_text(unit)

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
            capture_output=True, text=True,
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
    workctx_bin = _resolve_workctx_bin()
    task_name = _windows_task_name(config)

    if " " in workctx_bin:
        parts = workctx_bin.split(" ", 1)
        exe = parts[0]
        args = f"{parts[1]} daemon --config \"{config_path.resolve()}\""
    else:
        exe = workctx_bin
        args = f"daemon --config \"{config_path.resolve()}\""

    cmd = [
        "schtasks", "/create",
        "/tn", task_name,
        "/tr", f'"{exe}" {args}',
        "/sc", "ONLOGON",
        "/rl", "LIMITED",
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
        capture_output=True, text=True,
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
    workctx_bin = _resolve_workctx_bin()
    label = _plist_label(config)
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if " " in workctx_bin:
        parts = workctx_bin.split(" ", 1)
        program_args = [
            parts[0], parts[1], "sync",
            "--config", str(config_path.resolve()),
        ]
    else:
        program_args = [
            workctx_bin, "sync", "--config", str(config_path.resolve()),
        ]

    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": program_args,
        "StartCalendarInterval": {
            "Hour": config.schedule.hour,
            "Minute": config.schedule.minute,
        },
        "StandardOutPath": str(log_dir / "launchd-stdout.log"),
        "StandardErrorPath": str(log_dir / "launchd-stderr.log"),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        },
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
    try:
        subprocess.run(
            ["launchctl", "load", str(plist_file)],
            capture_output=True, timeout=10,
        )
    except Exception:
        logger.warning("Failed to load plist", exc_info=True)


def _unload_if_loaded(label: str, plist_file: Path) -> None:
    if plist_file.exists():
        with contextlib.suppress(Exception):
            subprocess.run(
                ["launchctl", "unload", str(plist_file)],
                capture_output=True, timeout=10,
            )


def _is_loaded(label: str) -> bool:
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=10,
        )
        return label in result.stdout
    except Exception:
        return False
