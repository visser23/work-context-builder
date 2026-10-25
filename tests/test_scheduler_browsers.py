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
