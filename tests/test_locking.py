"""Tests for execution locking."""

import json
import os

import pytest

from workctx.locking import ExecutionLock, LockError


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "test.lock"


def test_acquire_release(lock_path):
    lock = ExecutionLock(lock_path)
    lock.acquire()
    assert lock_path.exists()
    info = json.loads(lock_path.read_text())
    assert info["pid"] == os.getpid()

    lock.release()
    assert not lock_path.exists()


def test_context_manager(lock_path):
    with ExecutionLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_double_acquire_fails(lock_path):
    lock1 = ExecutionLock(lock_path)
    lock1.acquire()

    lock2 = ExecutionLock(lock_path)
    with pytest.raises(LockError):
        lock2.acquire()

    lock1.release()


def test_stale_lock_detected(lock_path):
    lock_path.write_text(json.dumps({"pid": 99999999, "started_at": "2020-01-01T00:00:00+00:00"}))

    lock = ExecutionLock(lock_path)
    lock.acquire()
    assert lock_path.exists()
    info = json.loads(lock_path.read_text())
    assert info["pid"] == os.getpid()
    lock.release()


def test_stale_lock_dead_pid(lock_path):
    lock_path.write_text(json.dumps({"pid": 99999999, "started_at": "2026-09-01T00:00:00+00:00"}))

    lock = ExecutionLock(lock_path)
    lock.acquire()
    lock.release()
