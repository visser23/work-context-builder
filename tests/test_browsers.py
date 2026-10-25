"""Tests for ``workctx.browsers``: Playwright's browser must never live in a cache dir."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from workctx import browsers


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    # setenv-then-delenv so monkeypatch restores "unset" even if the code under test sets it
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "placeholder")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    monkeypatch.setattr(browsers, "app_data_dir", lambda: tmp_path / "WorkContextMirror")


class TestBrowsersDir:
    def test_default_lives_beside_profiles_not_in_a_cache(self, tmp_path):
        d = browsers.browsers_dir()
        assert d == tmp_path / "WorkContextMirror" / "ms-playwright"
        assert not browsers.is_cache_path(d)

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/me/Library/Caches/ms-playwright",
            "/home/me/.cache/ms-playwright",
        ],
    )
    def test_cache_like_paths_are_detected(self, path):
        assert browsers.is_cache_path(Path(path))

    def test_persistent_paths_are_not_cache_like(self):
        assert not browsers.is_cache_path(
            Path("/Users/me/Library/Application Support/WorkContextMirror/ms-playwright")
        )


class TestConfigureBrowsersPath:
    def test_sets_env_when_unset(self, monkeypatch):
        import os

        result = browsers.configure_browsers_path()
        assert result == browsers.browsers_dir()
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers.browsers_dir())

    def test_respects_explicit_persistent_path(self, monkeypatch, tmp_path):
        import os

        mine = tmp_path / "my-browsers"
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(mine))
        assert browsers.configure_browsers_path() == mine
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(mine)

    def test_overrides_explicit_cache_path(self, monkeypatch):
        import os

        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/Users/me/Library/Caches/ms-playwright")
        result = browsers.configure_browsers_path()
        assert result == browsers.browsers_dir()
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers.browsers_dir())

    def test_zero_means_inside_package_and_is_kept(self, monkeypatch):
        import os

        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
        browsers.configure_browsers_path()
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "0"

    def test_playwright_env_carries_the_path(self):
        env = browsers.playwright_env()
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers.browsers_dir())


class TestEnsureChromium:
    def _pw(self, exe: object) -> SimpleNamespace:
        return SimpleNamespace(chromium=SimpleNamespace(executable_path=exe))

    def test_noop_when_executable_exists(self, tmp_path, monkeypatch):
        exe = tmp_path / "chrome"
        exe.write_text("x")
        monkeypatch.setattr(
            browsers, "install_chromium", lambda: pytest.fail("must not reinstall")
        )
        browsers.ensure_chromium(self._pw(str(exe)))

    def test_installs_when_missing_then_succeeds(self, tmp_path, monkeypatch):
        exe = tmp_path / "chrome"
        calls: list[int] = []

        def fake_install() -> None:
            calls.append(1)
            exe.write_text("x")

        monkeypatch.setattr(browsers, "install_chromium", fake_install)
        browsers.ensure_chromium(self._pw(str(exe)))
        assert calls == [1]

    def test_raises_clear_error_if_still_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(browsers, "install_chromium", lambda: None)
        with pytest.raises(RuntimeError, match="playwright install chromium"):
            browsers.ensure_chromium(self._pw(str(tmp_path / "nope")))

    def test_ignores_objects_without_a_real_path(self, monkeypatch):
        """Test doubles / unknown Playwright versions: do not try to guess."""
        monkeypatch.setattr(
            browsers, "install_chromium", lambda: pytest.fail("must not install")
        )
        browsers.ensure_chromium(self._pw(object()))
        browsers.ensure_chromium(SimpleNamespace())


class TestInstallChromium:
    def test_runs_playwright_install_with_persistent_path(self, monkeypatch):
        seen: dict = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen["env"] = kw.get("env", {})
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(browsers.subprocess, "run", fake_run)
        browsers.install_chromium()
        assert seen["cmd"][:4] == [sys.executable, "-m", "playwright", "install"]
        assert "chromium" in seen["cmd"]
        assert seen["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers.browsers_dir())

    def test_failure_raises_with_stderr(self, monkeypatch):
        monkeypatch.setattr(
            browsers.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "network down"),
        )
        with pytest.raises(RuntimeError, match="network down"):
            browsers.install_chromium()
