"""Atomic, `O_EXCL`-created single-run ownership lock (research.md §7).

Lives at `<ai_runs_dir>/.lock` — inside the directory already required to
be Git-ignored, so no new ignored path needs to be added. Refuses on
contention rather than queueing or silently retrying; never auto-removes
another process's lock file, even one that looks stale — recovery from a
truly stale lock is always an explicit, manual, human action.

**Ownership verification on release**: a lock payload carries a random
`owner_token` minted only by the instance that successfully created it.
`release()` re-reads the on-disk payload and refuses to unlink unless its
`owner_token` still matches the one this exact `RunLock` instance
recorded at acquisition — a second `RunLock` object (a different
"instance", whether a distinct process or merely a distinct object that
never itself acquired) can therefore never remove a lock it does not
demonstrably own, even one pointed at the same `.ai-runs/` directory.
This closes the gap where any instance could unconditionally `unlink()`
whatever currently sits at the lock path, live or not, its own or not.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from solari_workflow.errors import LockOwnershipError, WorkflowError
from solari_workflow.platform import proc

LOCK_SCHEMA_VERSION = "1"
LOCK_FILENAME = ".lock"


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class LockPayload:
    """The JSON payload written into the lock file on acquisition (research.md §7)."""

    pid: int
    hostname: str
    tool: str
    scope: str
    purpose: str
    started_at: str
    owner_token: str
    model: str | None = None
    effort: str | None = None
    session_mode: str | None = None
    lock_schema_version: str = LOCK_SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "LockPayload":
        data = json.loads(text)
        return cls(**data)


class LockContentionError(WorkflowError):
    """Another process already owns the run lock (research.md §7).

    The attempt is refused immediately, never queued or silently
    retried. `owner` is `None` only if the existing lock file could not
    itself be read/parsed (still a refusal, just with less detail).
    """

    def __init__(self, message: str, owner: LockPayload | None) -> None:
        super().__init__(message)
        self.owner = owner


def _liveness_note(owner: LockPayload) -> str:
    if owner.hostname != socket.gethostname():
        return "liveness could not be determined (lock held on a different host)"
    alive = proc.is_process_alive(owner.pid)
    if alive is True:
        return "process appears to still be running"
    if alive is False:
        return (
            "process does not appear to be running — if you have confirmed no "
            "workflow process is active, remove the lock file manually"
        )
    return "liveness could not be determined"


def _format_contention_message(owner: LockPayload | None, note: str) -> str:
    if owner is None:
        return (
            "WORKFLOW LOCKED — another execution owns this run, but its lock "
            f"file could not be read ({note}); resolve manually."
        )
    return (
        "WORKFLOW LOCKED — another execution owns this run: "
        f"tool={owner.tool} pid={owner.pid} hostname={owner.hostname} "
        f"purpose={owner.purpose!r} started_at={owner.started_at} ({note})"
    )


def peek_lock(ai_runs_dir: Path) -> LockPayload | None:
    """Read-only inspection of whatever currently sits at `<ai_runs_dir>/.lock`,
    without acquiring or releasing anything (`solari-workflow status`, T050 -
    "never mutates anything"). Returns `None` both when no lock file exists
    and when one exists but cannot be parsed (a status report has no
    stronger claim to make about an unreadable payload than "no usable
    lock information" - it never raises, since status must stay safe to
    run regardless of state).
    """
    path = ai_runs_dir / LOCK_FILENAME
    try:
        return LockPayload.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


@dataclass
class RunLock:
    """A run lock scoped to one `.ai-runs/` directory.

    Not frozen: a successful `acquire()` records this instance's own
    `owner_token` in `_owner_token` (private, excluded from equality) so
    a later `release()` on this same instance can verify it still owns
    the lock before deleting anything. A `RunLock` object that has never
    itself successfully acquired the lock always has `_owner_token is
    None`, and `release()` on it is an inert no-op — it can never delete
    a lock, live or stale, that it did not itself create.
    """

    ai_runs_dir: Path
    _owner_token: str | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def path(self) -> Path:
        return self.ai_runs_dir / LOCK_FILENAME

    def _read_owner(self) -> LockPayload | None:
        try:
            return LockPayload.from_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def acquire(
        self,
        *,
        tool: str,
        scope: str,
        purpose: str,
        model: str | None = None,
        effort: str | None = None,
        session_mode: str | None = None,
    ) -> LockPayload:
        """Atomically create the lock file, or raise `LockContentionError`.

        Uses `os.open(..., O_CREAT | O_EXCL)` (atomic on POSIX and
        Windows via Python's own `os` module) so two concurrent
        invocations racing to acquire cannot both succeed — no
        check-then-write TOCTOU window. On success, this instance's
        freshly minted `owner_token` is recorded so a later `release()`
        on this same instance can prove ownership.
        """
        payload = LockPayload(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            tool=tool,
            scope=scope,
            purpose=purpose,
            started_at=_utc_now_iso(),
            owner_token=uuid.uuid4().hex,
            model=model,
            effort=effort,
            session_mode=session_mode,
        )
        self.ai_runs_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            owner = self._read_owner()
            note = _liveness_note(owner) if owner is not None else "lock file unreadable"
            raise LockContentionError(_format_contention_message(owner, note), owner=owner) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload.to_json())
        self._owner_token = payload.owner_token
        return payload

    def release(self) -> None:
        """Remove the lock file, but only if this instance demonstrably owns it.

        1. If this instance never successfully acquired the lock (no
           recorded `owner_token`), this is a pure no-op — there is
           nothing for it to release, and it never inspects or touches
           whatever currently sits at the lock path. This alone is what
           stops a second `RunLock` instance (e.g. one that lost a
           `LockContentionError` race) from ever removing a live first
           instance's lock.
        2. If the lock file is already gone, releasing is treated as
           already having happened — idempotent, since there is nothing
           left to delete and therefore nothing that could be someone
           else's lock.
        3. Otherwise, the current on-disk payload is read and its
           `owner_token` compared against this instance's own. Only an
           exact match is unlinked. A mismatch — the lock was externally
           removed and replaced since this instance's `acquire()`, e.g.
           by another process after a manual deletion — raises
           `LockOwnershipError` rather than deleting someone else's
           live lock; the old owner can never remove a lock it no
           longer holds.
        4. A best-effort inode-identity re-check immediately before the
           unlink narrows (never fully eliminates, given stdlib-only
           filesystem primitives) the unavoidable check-then-unlink
           TOCTOU window: if the path was replaced again between the
           read above and this point, the unlink is refused rather than
           risking deletion of whatever now occupies the path.

        Calling `release()` again after a successful release is a
        further no-op (case 2) — the only idempotency this method
        guarantees is "releasing what you already released, or never
        held, is safe"; releasing a lock this instance can no longer
        verify as its own is never silently treated as success.
        """
        if self._owner_token is None:
            return
        try:
            fd = os.open(str(self.path), os.O_RDONLY)
        except FileNotFoundError:
            return
        try:
            stat_before = os.fstat(fd)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise LockOwnershipError(
                f"cannot verify ownership of {self.path} before release "
                f"(lock payload unreadable: {exc}); refusing to delete"
            ) from exc

        try:
            owner = LockPayload.from_json(text)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise LockOwnershipError(
                f"cannot verify ownership of {self.path} before release "
                f"(lock payload unparsable: {exc}); refusing to delete"
            ) from exc

        if owner.owner_token != self._owner_token:
            raise LockOwnershipError(
                f"refusing to release {self.path}: it is now owned by a "
                f"different instance (tool={owner.tool} pid={owner.pid} "
                f"hostname={owner.hostname}); this instance's own lock was "
                "already released or replaced by someone else"
            )

        try:
            stat_now = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            self._owner_token = None
            return
        if (stat_now.st_ino, stat_now.st_dev) != (stat_before.st_ino, stat_before.st_dev):
            raise LockOwnershipError(
                f"refusing to release {self.path}: it was replaced during "
                "release (identity changed); refusing to delete a lock this "
                "instance can no longer verify"
            )

        os.unlink(self.path)
        self._owner_token = None

    @contextlib.contextmanager
    def held(
        self,
        *,
        tool: str,
        scope: str,
        purpose: str,
        model: str | None = None,
        effort: str | None = None,
        session_mode: str | None = None,
    ) -> Iterator[LockPayload]:
        """Acquire for the duration of the `with` block, releasing in a `finally`.

        Every CLI subcommand acquires the run lock for its own duration
        only and releases it on exit, success or failure
        (contracts/cli-interface.md).
        """
        payload = self.acquire(
            tool=tool, scope=scope, purpose=purpose, model=model, effort=effort, session_mode=session_mode
        )
        try:
            yield payload
        finally:
            self.release()
