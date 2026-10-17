"""launchd-started processes get "do not download cloud placeholders on access" on macOS.

Every touch of a dataless OneDrive/iCloud file or folder then fails with EDEADLK
("Resource deadlock avoided"). The daemon opts back in at start-up.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

from workctx import macos_fs


class FakeLibc:
    def __init__(self, current=1, set_result=0):
        self.current = current
        self.set_result = set_result
        self.set_calls = []

    def getiopolicy_np(self, kind, scope):
        return self.current

    def setiopolicy_np(self, kind, scope, value):
        self.set_calls.append((kind, scope, value))
        if self.set_result == 0:
            self.current = value
        return self.set_result


def test_noop_on_other_platforms(monkeypatch):
    monkeypatch.setattr(macos_fs.sys, "platform", "linux")
    libc = FakeLibc()
    assert macos_fs.enable_dataless_materialization(libc=libc) is False
    assert libc.set_calls == []


def test_turns_materialization_on_when_it_is_off(monkeypatch):
    monkeypatch.setattr(macos_fs.sys, "platform", "darwin")
    libc = FakeLibc(current=macos_fs.IOPOL_MATERIALIZE_DATALESS_FILES_OFF)
    assert macos_fs.enable_dataless_materialization(libc=libc) is True
    assert libc.set_calls == [
        (
            macos_fs.IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
            macos_fs.IOPOL_SCOPE_PROCESS,
            macos_fs.IOPOL_MATERIALIZE_DATALESS_FILES_ON,
        )
    ]


def test_leaves_it_alone_when_already_on(monkeypatch):
    monkeypatch.setattr(macos_fs.sys, "platform", "darwin")
    libc = FakeLibc(current=macos_fs.IOPOL_MATERIALIZE_DATALESS_FILES_ON)
    assert macos_fs.enable_dataless_materialization(libc=libc) is True
    assert libc.set_calls == []


def test_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(macos_fs.sys, "platform", "darwin")
    libc = FakeLibc(current=1, set_result=-1)
    assert macos_fs.enable_dataless_materialization(libc=libc) is False


def test_missing_libc_symbols_are_reported_not_raised(monkeypatch):
    monkeypatch.setattr(macos_fs.sys, "platform", "darwin")
    assert macos_fs.enable_dataless_materialization(libc=SimpleNamespace()) is False


def test_run_sync_enables_it(monkeypatch, tmp_path):
    """run_sync is the single entry point for the daemon and the CLI."""
    import workctx.sync as sync

    called = []
    monkeypatch.setattr(sync, "enable_dataless_materialization", lambda: called.append(1) or True)
    cfg = SimpleNamespace(state_dir=tmp_path)  # lock acquire fails fast below
    monkeypatch.setattr(
        sync, "ExecutionLock", lambda *_: (_ for _ in ()).throw(RuntimeError("stop here"))
    )
    with contextlib.suppress(RuntimeError):
        sync.run_sync(cfg, run_id="t", quiet=True)
    assert called == [1]
