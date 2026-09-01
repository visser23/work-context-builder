"""macOS launchd scheduler management."""

from __future__ import annotations

import contextlib
import logging
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from workctx.config import ProjectConfig

logger = logging.getLogger(__name__)

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PREFIX = "com.workctx"


def _plist_label(config: ProjectConfig) -> str:
    return f"{PLIST_PREFIX}.{config.project.id}"


def _plist_path(config: ProjectConfig) -> Path:
    return LAUNCH_AGENTS_DIR / f"{_plist_label(config)}.plist"


def install_schedule(config: ProjectConfig, config_path: Path) -> Path:
    """Install a launchd user agent for scheduled sync."""
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    workctx_bin = shutil.which("workctx")
    if not workctx_bin:
        uv_bin = shutil.which("uv")
        if uv_bin:
            workctx_bin = f"{uv_bin} run workctx"
        else:
            raise RuntimeError(
                "Cannot find 'workctx' on PATH. "
                "Install the package first: uv pip install -e ."
            )

    label = _plist_label(config)
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if " " in workctx_bin:
        parts = workctx_bin.split(" ", 1)
        program_args = [parts[0], parts[1], "sync", "--config", str(config_path.resolve())]
    else:
        program_args = [workctx_bin, "sync", "--config", str(config_path.resolve())]

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
    """Remove the launchd schedule."""
    label = _plist_label(config)
    plist_file = _plist_path(config)

    _unload_if_loaded(label, plist_file)

    if plist_file.exists():
        plist_file.unlink()
        logger.info("Schedule removed: %s", plist_file)
    else:
        logger.info("No schedule file found at %s", plist_file)


def get_schedule_status(config: ProjectConfig) -> dict[str, Any]:
    """Get the current schedule status."""
    label = _plist_label(config)
    plist_file = _plist_path(config)

    result: dict[str, Any] = {
        "installed": plist_file.exists(),
        "plist_path": str(plist_file),
        "label": label,
    }

    if plist_file.exists():
        try:
            with open(plist_file, "rb") as f:
                plist = plistlib.load(f)
            cal = plist.get("StartCalendarInterval", {})
            result["time"] = f"{cal.get('Hour', '?'):02}:{cal.get('Minute', '?'):02}"
        except Exception:
            result["time"] = "unknown"

        loaded = _is_loaded(label)
        result["loaded"] = loaded

    return result


def _load_plist(plist_file: Path) -> None:
    try:
        subprocess.run(
            ["launchctl", "load", str(plist_file)],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        logger.warning("Failed to load plist", exc_info=True)


def _unload_if_loaded(label: str, plist_file: Path) -> None:
    if plist_file.exists():
        with contextlib.suppress(Exception):
            subprocess.run(
                ["launchctl", "unload", str(plist_file)],
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
