"""The service must pin Playwright's browser to a persistent (non-cache) directory."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from workctx import browsers, scheduler


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "placeholder")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    monkeypatch.setattr(browsers, "app_data_dir", lambda: tmp_path / "WorkContextMirror")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def _config() -> MagicMock:
    config = MagicMock()
    config.project.id = "test"
    return config


def test_service_command_pins_browser_path(tmp_path):
    cfg_path = tmp_path / "workctx.yaml"
    cfg_path.write_text("x")
    _args, env = scheduler._build_service_command(_config(), cfg_path, "daemon")
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers.browsers_dir())
    assert not browsers.is_cache_path(Path(env["PLAYWRIGHT_BROWSERS_PATH"]))


def test_login_wrapper_exports_browser_path(tmp_path):
    scheduler._create_login_wrapper(_config(), tmp_path / "venv", tmp_path / "workctx.yaml")
    wrapper = (tmp_path / ".local" / "bin" / "workctx-relogin").read_text()
    assert f'export PLAYWRIGHT_BROWSERS_PATH="{browsers.browsers_dir()}"' in wrapper
    assert wrapper.index("export PLAYWRIGHT_BROWSERS_PATH") < wrapper.index("exec ")


def test_browser_install_uses_persistent_path(monkeypatch, tmp_path):
    """``_prepare_local_install`` installs chromium with the pinned env, not the cache."""
    cfg_path = tmp_path / "proj" / "workctx.yaml"
    cfg_path.parent.mkdir()
    cfg_path.write_text("x")
    monkeypatch.setattr(scheduler.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(scheduler, "_create_login_wrapper", lambda *a, **k: None)

    seen_envs: list[dict] = []

    def fake_run(cmd, **kw):
        if cmd[0].endswith("playwright"):
            seen_envs.append(kw.get("env") or {})
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1] == "pip":  # make the venv's playwright binary "exist"
            pw = scheduler._local_daemon_venv_dir(_config()) / "bin" / "playwright"
            pw.parent.mkdir(parents=True, exist_ok=True)
            pw.write_text("")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    scheduler._prepare_local_install(_config(), cfg_path)
    assert seen_envs, "playwright install was not run"
    assert seen_envs[0]["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers.browsers_dir())


# ------------------------------------------------ the `workctx` launcher on PATH


def test_install_creates_a_workctx_launcher_that_knows_the_config(tmp_path):
    cfg = tmp_path / "workctx.yaml"
    scheduler._create_login_wrapper(_config(), tmp_path / "venv", cfg)
    launcher = (tmp_path / ".local" / "bin" / "workctx").read_text()
    assert f'export PLAYWRIGHT_BROWSERS_PATH="{browsers.browsers_dir()}"' in launcher
    assert f'WORKCTX_CONFIG="${{WORKCTX_CONFIG:-{cfg}}}"' in launcher  # user env wins
    assert (
        launcher.rstrip().endswith('"$@"')
        and str(tmp_path / "venv" / "bin" / "workctx") in launcher
    )
    assert (tmp_path / ".local" / "bin" / "workctx").stat().st_mode & 0o111


def test_relogin_wrapper_runs_the_smart_relogin_command(tmp_path):
    scheduler._create_login_wrapper(_config(), tmp_path / "venv", tmp_path / "workctx.yaml")
    wrapper = (tmp_path / ".local" / "bin" / "workctx-relogin").read_text()
    assert "auth relogin" in wrapper and "login-sharepoint" not in wrapper


def test_a_foreign_workctx_on_the_path_is_never_overwritten(tmp_path):
    foreign = tmp_path / ".local" / "bin" / "workctx"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("#!/bin/sh\necho somebody else's tool\n")
    scheduler._create_login_wrapper(_config(), tmp_path / "venv", tmp_path / "workctx.yaml")
    assert "somebody else's tool" in foreign.read_text()
    # ...but our own earlier launcher is refreshed in place
    scheduler._create_login_wrapper(_config(), tmp_path / "venv2", tmp_path / "workctx.yaml")
    assert "somebody else's tool" in foreign.read_text()


def test_our_own_launcher_is_refreshed_on_reinstall(tmp_path):
    scheduler._create_login_wrapper(_config(), tmp_path / "venv", tmp_path / "workctx.yaml")
    scheduler._create_login_wrapper(_config(), tmp_path / "venv2", tmp_path / "workctx.yaml")
    assert "venv2" in (tmp_path / ".local" / "bin" / "workctx").read_text()


def test_uv_launcher_for_projects_not_on_cloud_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scheduler.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    scheduler.create_cli_launchers(_config(), proj / "workctx.yaml")
    launcher = (tmp_path / ".local" / "bin" / "workctx").read_text()
    assert "/usr/bin/uv" in launcher and "run" in launcher and str(proj) in launcher


def test_no_uv_launcher_when_workctx_is_already_on_the_path(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler.shutil, "which", lambda name: f"/opt/bin/{name}")
    scheduler.create_cli_launchers(_config(), tmp_path / "workctx.yaml")
    assert not (tmp_path / ".local" / "bin" / "workctx").exists()
