"""Unit tests for `lock/run_lock.py` (T015).

Covers single-process acquire/release, contention refusal, and stale-
liveness annotation. The real two-process contention integration test
is Phase 5 (US3, T052) — out of scope for this Foundational block; these
tests exercise the same primitives at the unit level.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from solari_workflow.errors import LockOwnershipError
from solari_workflow.lock.run_lock import LockContentionError, RunLock


def test_acquire_creates_lock_file_with_expected_payload(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    payload = lock.acquire(tool="claude", scope="T001-T003", purpose="implement block")

    assert lock.path.is_file()
    on_disk = json.loads(lock.path.read_text(encoding="utf-8"))
    assert on_disk["tool"] == "claude"
    assert on_disk["scope"] == "T001-T003"
    assert on_disk["purpose"] == "implement block"
    assert on_disk["pid"] == payload.pid
    assert on_disk["lock_schema_version"] == "1"


def test_release_removes_the_lock_file(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.acquire(tool="codex", scope="T001", purpose="gate")
    lock.release()
    assert not lock.path.exists()


def test_release_is_a_no_op_when_no_lock_is_held(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.release()  # must not raise


def test_held_context_manager_releases_on_success(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    with lock.held(tool="claude", scope="T001", purpose="implement"):
        assert lock.path.exists()
    assert not lock.path.exists()


def test_held_context_manager_releases_on_exception(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    with pytest.raises(RuntimeError):
        with lock.held(tool="claude", scope="T001", purpose="implement"):
            assert lock.path.exists()
            raise RuntimeError("boom")
    assert not lock.path.exists()


def test_second_acquire_is_refused_and_names_the_owner(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.acquire(tool="claude", scope="T001", purpose="first run")

    with pytest.raises(LockContentionError) as excinfo:
        lock.acquire(tool="codex", scope="T001", purpose="second run")

    assert excinfo.value.owner is not None
    assert excinfo.value.owner.tool == "claude"
    assert excinfo.value.owner.purpose == "first run"
    assert "first run" in str(excinfo.value)
    assert "claude" in str(excinfo.value)


def test_contention_never_removes_the_existing_lock(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.acquire(tool="claude", scope="T001", purpose="first run")

    with pytest.raises(LockContentionError):
        lock.acquire(tool="codex", scope="T001", purpose="second run")

    assert lock.path.exists()  # the runner never auto-removes another owner's lock


def test_liveness_annotation_reports_alive_for_the_current_process(tmp_path: Path) -> None:
    import os
    import socket

    lock = RunLock(ai_runs_dir=tmp_path)
    lock.ai_runs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "tool": "claude",
        "scope": "T001",
        "purpose": "still running",
        "started_at": "2026-01-01T00:00:00Z",
        "owner_token": "external-owner-token-alive",
        "model": None,
        "effort": None,
        "session_mode": None,
        "lock_schema_version": "1",
    }
    lock.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LockContentionError) as excinfo:
        lock.acquire(tool="codex", scope="T001", purpose="second run")

    assert "appears to still be running" in str(excinfo.value)


def test_liveness_annotation_reports_not_running_for_an_unlikely_pid(tmp_path: Path) -> None:
    import socket

    lock = RunLock(ai_runs_dir=tmp_path)
    lock.ai_runs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": 2**30,
        "hostname": socket.gethostname(),
        "tool": "claude",
        "scope": "T001",
        "purpose": "crashed run",
        "started_at": "2026-01-01T00:00:00Z",
        "owner_token": "external-owner-token-crashed",
        "model": None,
        "effort": None,
        "session_mode": None,
        "lock_schema_version": "1",
    }
    lock.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LockContentionError) as excinfo:
        lock.acquire(tool="codex", scope="T001", purpose="second run")

    assert "does not appear to be running" in str(excinfo.value)
    assert "remove the lock file manually" in str(excinfo.value)
    # Still never auto-removed, even though it looks stale.
    assert lock.path.exists()


def test_liveness_annotation_reports_undeterminable_for_a_different_host(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.ai_runs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": 12345,
        "hostname": "some-other-machine",
        "tool": "claude",
        "scope": "T001",
        "purpose": "remote run",
        "started_at": "2026-01-01T00:00:00Z",
        "owner_token": "external-owner-token-remote",
        "model": None,
        "effort": None,
        "session_mode": None,
        "lock_schema_version": "1",
    }
    lock.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LockContentionError) as excinfo:
        lock.acquire(tool="codex", scope="T001", purpose="second run")

    assert "liveness could not be determined" in str(excinfo.value)


def test_contention_with_unreadable_lock_file_still_refuses(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.ai_runs_dir.mkdir(parents=True, exist_ok=True)
    lock.path.write_text("not valid json", encoding="utf-8")

    with pytest.raises(LockContentionError) as excinfo:
        lock.acquire(tool="codex", scope="T001", purpose="second run")

    assert excinfo.value.owner is None
    assert lock.path.exists()


# --- Ownership-verified release (remediation of the RunLock BLOCKING finding) ---


def test_owner_can_release_its_own_lock(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.acquire(tool="claude", scope="T001", purpose="implement")
    lock.release()
    assert not lock.path.exists()


def test_instance_that_never_acquired_cannot_release_an_existing_foreign_lock(tmp_path: Path) -> None:
    """An instance with no `owner_token` of its own must never touch a lock
    it did not itself create — regardless of who currently owns it."""
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.ai_runs_dir.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "hostname": "someone-elses-host",
                "tool": "codex",
                "scope": "T001",
                "purpose": "someone else's run",
                "started_at": "2026-01-01T00:00:00Z",
                "owner_token": "not-mine",
                "model": None,
                "effort": None,
                "session_mode": None,
                "lock_schema_version": "1",
            }
        ),
        encoding="utf-8",
    )

    lock.release()  # must not raise — and must not touch the file

    assert lock.path.exists()
    on_disk = json.loads(lock.path.read_text(encoding="utf-8"))
    assert on_disk["owner_token"] == "not-mine"


def test_second_instance_cannot_remove_a_live_first_instances_lock(tmp_path: Path) -> None:
    first = RunLock(ai_runs_dir=tmp_path)
    first.acquire(tool="claude", scope="T001", purpose="first run")

    second = RunLock(ai_runs_dir=tmp_path)
    with pytest.raises(LockContentionError):
        second.acquire(tool="codex", scope="T001", purpose="second run")

    second.release()  # never acquired anything itself — must be inert
    assert first.path.exists()

    first.release()  # the true owner can still release its own lock
    assert not first.path.exists()


def test_externally_replaced_lock_cannot_be_deleted_by_the_old_owner(tmp_path: Path) -> None:
    """Simulates the lock being manually deleted and re-created by a
    different (real) process while the old owner still believes it holds
    the lock — the old owner must refuse to delete the new one."""
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.acquire(tool="claude", scope="T001", purpose="original run")

    # Someone/something else removed and re-created the lock file out of band.
    lock.path.unlink()
    replacement_payload = {
        "pid": 424242,
        "hostname": socket.gethostname(),
        "tool": "codex",
        "scope": "T002",
        "purpose": "a completely different run",
        "started_at": "2026-02-02T00:00:00Z",
        "owner_token": "replacement-owner-token",
        "model": None,
        "effort": None,
        "session_mode": None,
        "lock_schema_version": "1",
    }
    lock.path.write_text(json.dumps(replacement_payload), encoding="utf-8")

    with pytest.raises(LockOwnershipError):
        lock.release()

    # The replacement lock must survive untouched.
    assert lock.path.exists()
    on_disk = json.loads(lock.path.read_text(encoding="utf-8"))
    assert on_disk["owner_token"] == "replacement-owner-token"


def test_stale_looking_foreign_lock_is_not_auto_deleted_by_release(tmp_path: Path) -> None:
    """Even a lock whose liveness annotation would call it "stale" (an
    implausible PID) is never removed by an instance that does not own it —
    stale-lock recovery stays manual-only, including at release time."""
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.ai_runs_dir.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps(
            {
                "pid": 2**30,
                "hostname": socket.gethostname(),
                "tool": "claude",
                "scope": "T001",
                "purpose": "crashed run",
                "started_at": "2026-01-01T00:00:00Z",
                "owner_token": "stale-owner-token",
                "model": None,
                "effort": None,
                "session_mode": None,
                "lock_schema_version": "1",
            }
        ),
        encoding="utf-8",
    )

    lock.release()  # this instance never acquired it — must not delete

    assert lock.path.exists()


def test_release_is_idempotent_after_a_successful_release(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.acquire(tool="claude", scope="T001", purpose="implement")
    lock.release()
    lock.release()  # must not raise — nothing left to release
    assert not lock.path.exists()


def test_release_is_not_idempotent_across_an_ownership_mismatch(tmp_path: Path) -> None:
    """Once a mismatch is detected, calling release() again keeps refusing —
    it never eventually "gives up" and deletes the file anyway."""
    lock = RunLock(ai_runs_dir=tmp_path)
    lock.acquire(tool="claude", scope="T001", purpose="original run")
    lock.path.unlink()
    lock.path.write_text(
        json.dumps(
            {
                "pid": 1,
                "hostname": socket.gethostname(),
                "tool": "codex",
                "scope": "T002",
                "purpose": "someone else",
                "started_at": "2026-01-01T00:00:00Z",
                "owner_token": "someone-elses-token",
                "model": None,
                "effort": None,
                "session_mode": None,
                "lock_schema_version": "1",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LockOwnershipError):
        lock.release()
    with pytest.raises(LockOwnershipError):
        lock.release()

    assert lock.path.exists()


def test_held_context_manager_releases_its_own_lock_only(tmp_path: Path) -> None:
    lock = RunLock(ai_runs_dir=tmp_path)
    with lock.held(tool="claude", scope="T001", purpose="implement") as payload:
        on_disk = json.loads(lock.path.read_text(encoding="utf-8"))
        assert on_disk["owner_token"] == payload.owner_token
    assert not lock.path.exists()
