"""Single-instance lock: захват, отказ при живом свежем локе, перехват протухшего."""

import json
import time

import pytest

from analyzer import lock as lockmod
from analyzer.lock import LockHeld, SingleInstanceLock


def test_acquire_on_empty_creates_file(tmp_path):
    lk = SingleInstanceLock(tmp_path / "a.lock")
    assert lk.acquire() is True
    assert (tmp_path / "a.lock").exists()


def test_refused_when_held_by_alive_fresh(tmp_path, monkeypatch):
    p = tmp_path / "a.lock"
    p.write_text(json.dumps({"pid": 99999, "ts": 1000.0}), encoding="utf-8")
    monkeypatch.setattr(lockmod, "_pid_alive", lambda pid: True)
    lk = SingleInstanceLock(p, stale_after_s=300)
    assert lk.acquire(now=1100.0) is False        # свежий (age 100 < 300) и жив → занят


def test_stolen_when_pid_dead(tmp_path, monkeypatch):
    p = tmp_path / "a.lock"
    p.write_text(json.dumps({"pid": 99999, "ts": 1000.0}), encoding="utf-8")
    monkeypatch.setattr(lockmod, "_pid_alive", lambda pid: False)
    lk = SingleInstanceLock(p, stale_after_s=300)
    assert lk.acquire(now=1100.0) is True         # процесс мёртв (POSIX) → перехват


def test_stolen_when_heartbeat_stale(tmp_path, monkeypatch):
    p = tmp_path / "a.lock"
    p.write_text(json.dumps({"pid": 99999, "ts": 1000.0}), encoding="utf-8")
    monkeypatch.setattr(lockmod, "_pid_alive", lambda pid: None)  # Windows-like: liveness неизвестна
    lk = SingleInstanceLock(p, stale_after_s=300)
    assert lk.acquire(now=2000.0) is True         # age 1000 > 300 → протух по возрасту


def test_release_removes_only_own_lock(tmp_path):
    p = tmp_path / "a.lock"
    lk = SingleInstanceLock(p)
    lk.acquire()
    lk.release()
    assert not p.exists()


def test_context_manager_raises_when_held(tmp_path, monkeypatch):
    p = tmp_path / "a.lock"
    p.write_text(json.dumps({"pid": 99999, "ts": time.time()}), encoding="utf-8")  # свежий heartbeat
    monkeypatch.setattr(lockmod, "_pid_alive", lambda pid: True)
    with pytest.raises(LockHeld):
        with SingleInstanceLock(p, stale_after_s=300):
            pass
