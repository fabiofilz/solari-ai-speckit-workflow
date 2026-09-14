"""`.ai-runs/.block-state.json`: state vocabulary, retry ceiling, resume
foundation (data-model.md's "Block State", research.md §0.7).

A **fast-path cache, not the sole source of truth** — `HISTORY-BASED
REGENERATION` below reconstructs an equivalent record from the permanent
`.ai-runs/*-result.md` audit trail (`runs/audit.py`, T016, already
implemented) and current Git state when the JSON file is missing or looks
stale, per research.md §0.7's own "Fallback / kept simple" note. This
module owns exactly the persistent record and its regeneration primitive;
composing it into `resume`/`status` (deciding *when* to regenerate, and
what to do once state is known) is T046/T050 — a later phase of this
feature, not implemented here.

**No autonomous continuation of any kind lives here** — this module never
starts, retries, or resumes a stage on its own; it only reads and writes a
small JSON record and reconstructs one, read-only, from disk when asked.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solari_workflow.fingerprint.engine import Fingerprint, fingerprint_from_dict, fingerprint_to_dict
from solari_workflow.git.branch import parse_branch_name
from solari_workflow.git.ops import GitRepo

BLOCK_STATE_FILENAME = ".block-state.json"
COMPLETION_GUARD_FILENAME = ".block-state.completion-guard.json"
COMPLETION_RECEIPT_FILENAME = ".block-state.completion-receipt.json"

# The entire state vocabulary (research.md §0.7) — no other values exist.
STATE_VALUES: tuple[str, ...] = (
    "RUNNING",
    "QA_REMEDIATION_REQUIRED",
    "RETRYABLE_ERROR",
    "BLOCKED_MANUAL",
    "ARCHITECTURAL_DECISION_REQUIRED",
    "READY_TO_RESUME",
    "COMPLETED",
)

# The last stage proven to have completed correctly, in forward order —
# `resume` continues immediately after this stage, never before it
# (data-model.md's "Block State").
LAST_SAFE_STAGE_VALUES: tuple[str, ...] = (
    "BRANCH_CREATED",
    "IMPLEMENTATION_COMPLETE",
    "CANDIDATE_TREE_BUILT",
    "GATE_PASSED",
    "CHECKPOINT_COMPLETE",
)

RETRY_COUNT_CEILING = 1  # fixed, not configurable (research.md §0.6)


class BlockStateError(Exception):
    """A `.ai-runs/.block-state.json` payload (on disk, or constructed in
    memory) does not satisfy the Block State contract — an unrecognized
    `state`/`last_safe_stage` value, a `retry_count_current_stage` outside
    `{0, 1}`, a missing required field, or a structurally invalid on-disk
    file (not JSON, or not a JSON object). Raised rather than silently
    coerced or defaulted — a corrupt or invalid record must never be
    mistaken for a valid one that merely happens to be missing/empty."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PendingClaudeInvocation:
    """The normalized, Orchestrator-approved `run-claude` invocation
    parameters for the block's CURRENT, not-yet-`IMPLEMENTATION_COMPLETE`
    attempt (item 6/A3 remediation) — persisted so `resume` can execute
    the originally-authorized Claude stage from `BRANCH_CREATED` without
    inventing model/effort/session-mode values of its own.

    Only plain, non-secret, Orchestrator-supplied invocation parameters -
    never a secret, never a transient shell/process detail (no PID, no
    environment snapshot, no working-directory-specific path).
    """

    model: str
    effort: str
    session_mode: str
    session_id: str | None
    purpose: str


def _pending_claude_invocation_to_dict(value: PendingClaudeInvocation) -> dict[str, Any]:
    return asdict(value)


def _pending_claude_invocation_from_dict(data: dict[str, Any]) -> PendingClaudeInvocation:
    return PendingClaudeInvocation(
        model=str(data["model"]),
        effort=str(data["effort"]),
        session_mode=str(data["session_mode"]),
        session_id=None if data.get("session_id") is None else str(data["session_id"]),
        purpose=str(data["purpose"]),
    )


@dataclass(frozen=True)
class BlockState:
    """The full persistent record (data-model.md's "Block State").

    Validated on construction (`__post_init__`) — there is no way to hold
    a `BlockState` instance whose `state`/`last_safe_stage` is outside the
    fixed vocabularies above, or whose `retry_count_current_stage` exceeds
    the fixed single-retry ceiling.
    """

    branch_name: str
    first_task: str
    last_task: str
    block_name: str
    state: str
    last_safe_stage: str
    retry_count_current_stage: int
    last_gate: Fingerprint | None
    updated_at: str
    # The exact Spec Kit `tasks.md` path this block's task IDs/titles are
    # bound to (A5 remediation) — resolved and persisted ONCE, at
    # `start-block` time, so a later `run-codex-gate`/`checkpoint` uses
    # the identical task source rather than re-resolving it (and risking
    # a different, ambiguous answer if the repository's Spec Kit layout
    # changed mid-block). `None` for a block whose feature identity could
    # not be resolved (no `tasks.md` found at all — a legitimate absence,
    # per `speckit/integration.py`'s own "not present" handling) or for a
    # record predating this field / produced by lossy regeneration
    # (`regenerate_block_state`, below) — callers must treat `None` as
    # "task titles are unavailable", never as "safe to guess a path".
    # Declared with a default so existing/regenerated records that
    # predate this field still deserialize (`from_dict` does not require
    # this key).
    tasks_md_path: str | None = None
    # The `HEAD` commit oid recorded immediately after `start-block`
    # created and checked out the branch (item 6) - Claude never commits
    # (research.md §0.9), so this stays the expected `HEAD` through
    # `BRANCH_CREATED`/`IMPLEMENTATION_COMPLETE`; `resume` verifies
    # against it before proceeding from ANY stage, not merely `GATE_PASSED`.
    expected_head_oid: str | None = None
    # `main`'s own oid at the moment `run-codex-gate` last computed an
    # ELIGIBLE PASS (item 4/8) - `checkpoint` requires `main` to still be
    # at exactly this oid before its first mutation, proving the merge
    # base Codex reviewed has not silently moved. Cleared alongside
    # `last_gate` by `_revoke_prior_gate` (cli.py) on any non-eligible
    # outcome - never left stale.
    gated_main_oid: str | None = None
    # The block's own `tasks_md_path` AT THE MOMENT the current `last_gate`
    # became eligible (item 8's composed Block/Gate Identity: Fingerprint
    # X + canonical feature identity). Since `tasks_md_path` itself never
    # changes after `start-block`, this is normally identical to it by
    # construction - the explicit, independent comparison at checkpoint/
    # resume time is what actually PROVES the invariant rather than
    # merely assuming it, and is what would catch a corrupted/tampered
    # on-disk record. Cleared alongside `last_gate`.
    gate_feature_identity: str | None = None
    # The still-pending, Orchestrator-approved `run-claude` invocation for
    # this block's CURRENT attempt (item 6) - set before every `run-claude`
    # invocation while `last_safe_stage` has not yet reached
    # `IMPLEMENTATION_COMPLETE`, cleared once it does (or once an
    # `ARCHITECTURAL_DECISION_REQUIRED` stop requires fresh Orchestrator
    # instructions rather than a blind replay).
    pending_claude_invocation: PendingClaudeInvocation | None = None
    # Third remediation (M3): a REFERENCE to the immutable, structured
    # gate-identity evidence (`state/gate_identity.py`) - its filename
    # under the `.ai-runs/` directory and its SHA-256 digest. Cached here
    # only; `checkpoint` re-reads and verifies the evidence itself.
    # Cleared together with every other gate-bound field whenever a new
    # gate attempt begins.
    gate_identity_record: str | None = None
    gate_identity_sha256: str | None = None
    # Third remediation (M4/M5): the last mutation boundary a failed
    # checkpoint is known to have reached. Non-`None` means real Git
    # state may already have been mutated; `resume` and every lifecycle
    # command refuse to continue automatically until a human resolves it.
    checkpoint_mutation_boundary: str | None = None

    def __post_init__(self) -> None:
        if self.state not in STATE_VALUES:
            raise BlockStateError(
                f"invalid state {self.state!r}; must be one of {', '.join(STATE_VALUES)}"
            )
        if self.last_safe_stage not in LAST_SAFE_STAGE_VALUES:
            raise BlockStateError(
                f"invalid last_safe_stage {self.last_safe_stage!r}; "
                f"must be one of {', '.join(LAST_SAFE_STAGE_VALUES)}"
            )
        if self.retry_count_current_stage not in (0, RETRY_COUNT_CEILING):
            raise BlockStateError(
                "retry_count_current_stage must be 0 or "
                f"{RETRY_COUNT_CEILING} (the fixed single-retry ceiling), "
                f"got {self.retry_count_current_stage!r}"
            )


def _path(ai_runs_dir: Path) -> Path:
    return ai_runs_dir / BLOCK_STATE_FILENAME


def _strict_json_int(value: Any, *, field_name: str) -> int:
    """Accept only an actual JSON integer — never `int(value)` coercion.

    `bool` is an `int` subclass in Python (`isinstance(True, int)` is
    `True`), so `type(value) is int` (not `isinstance`) is required to
    reject `true`/`false`. A JSON float (`0.5`, or even `1.0`) must also
    be rejected outright — `int(1.0)` silently succeeding is exactly the
    persisted-state coercion this validation exists to close (T022-T032
    re-gate, Finding 4 — MINOR). Strings, `null`, and every other type are
    likewise rejected; only a genuine `int` reaches the caller.
    """
    if type(value) is not int:  # noqa: E721 - deliberate exact-type check, not isinstance
        raise BlockStateError(
            f"{field_name} must be a JSON integer, got {value!r} ({type(value).__name__})"
        )
    return value


def to_dict(state: BlockState) -> dict[str, Any]:
    """Plain-dict serialization, with `last_gate`/`pending_claude_invocation`
    rendered as nested dicts (or `None`) rather than raw dataclass instances."""
    data = asdict(state)
    data["last_gate"] = None if state.last_gate is None else fingerprint_to_dict(state.last_gate)
    data["pending_claude_invocation"] = (
        None
        if state.pending_claude_invocation is None
        else _pending_claude_invocation_to_dict(state.pending_claude_invocation)
    )
    return data


def from_dict(data: dict[str, Any]) -> BlockState:
    """Inverse of :func:`to_dict`. Raises :class:`BlockStateError` on any
    missing required key or malformed field, aggregating nothing —
    `BlockState.__post_init__` handles vocabulary validation once the
    fields are extracted; this function only handles shape/type
    extraction and reports the first structural problem it finds."""
    required = {
        "branch_name",
        "first_task",
        "last_task",
        "block_name",
        "state",
        "last_safe_stage",
        "retry_count_current_stage",
        "last_gate",
        "updated_at",
    }
    missing = required - data.keys()
    if missing:
        raise BlockStateError(f"missing required field(s): {', '.join(sorted(missing))}")

    last_gate_raw = data["last_gate"]
    try:
        last_gate = None if last_gate_raw is None else fingerprint_from_dict(last_gate_raw)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise BlockStateError(f"malformed last_gate payload: {exc}") from exc

    retry_count = _strict_json_int(
        data["retry_count_current_stage"], field_name="retry_count_current_stage"
    )

    # None of these newer fields are in `required` above: an existing
    # on-disk record from before they existed, or one produced by
    # `regenerate_block_state`, must still load (A5/item-6/item-8's own
    # "None means unavailable, never guessed" contract — see each
    # field's own comment on `BlockState`).
    tasks_md_path_raw = data.get("tasks_md_path")
    tasks_md_path = None if tasks_md_path_raw is None else str(tasks_md_path_raw)
    expected_head_oid_raw = data.get("expected_head_oid")
    expected_head_oid = None if expected_head_oid_raw is None else str(expected_head_oid_raw)
    gated_main_oid_raw = data.get("gated_main_oid")
    gated_main_oid = None if gated_main_oid_raw is None else str(gated_main_oid_raw)
    gate_feature_identity_raw = data.get("gate_feature_identity")
    gate_feature_identity = None if gate_feature_identity_raw is None else str(gate_feature_identity_raw)

    def _optional_str(key: str) -> str | None:
        value = data.get(key)
        return None if value is None else str(value)

    pending_raw = data.get("pending_claude_invocation")
    try:
        pending_claude_invocation = (
            None if pending_raw is None else _pending_claude_invocation_from_dict(pending_raw)
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise BlockStateError(f"malformed pending_claude_invocation payload: {exc}") from exc

    try:
        return BlockState(
            branch_name=str(data["branch_name"]),
            first_task=str(data["first_task"]),
            last_task=str(data["last_task"]),
            block_name=str(data["block_name"]),
            state=str(data["state"]),
            last_safe_stage=str(data["last_safe_stage"]),
            retry_count_current_stage=retry_count,
            last_gate=last_gate,
            updated_at=str(data["updated_at"]),
            tasks_md_path=tasks_md_path,
            expected_head_oid=expected_head_oid,
            gated_main_oid=gated_main_oid,
            gate_feature_identity=gate_feature_identity,
            pending_claude_invocation=pending_claude_invocation,
            gate_identity_record=_optional_str("gate_identity_record"),
            gate_identity_sha256=_optional_str("gate_identity_sha256"),
            checkpoint_mutation_boundary=_optional_str("checkpoint_mutation_boundary"),
        )
    except (TypeError, ValueError) as exc:
        raise BlockStateError(f"malformed block-state payload: {exc}") from exc


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BlockStateError(f"could not read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BlockStateError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BlockStateError(f"{path} must contain a JSON object, got {type(data).__name__}")
    return data


def load_block_state(ai_runs_dir: Path) -> BlockState | None:
    """Read the authoritative block state, applying any completion guard.

    Returns `None` only for the ordinary, legitimate case of no block ever
    having started (the file does not exist yet) — never for a file that
    exists but cannot be parsed/validated, which always raises
    :class:`BlockStateError` instead (fail-closed: a corrupt record must
    never be silently treated as "no active block")."""
    path = _path(ai_runs_dir)
    guard_path = ai_runs_dir / COMPLETION_GUARD_FILENAME
    receipt_path = ai_runs_dir / COMPLETION_RECEIPT_FILENAME
    if not path.is_file() and not guard_path.is_file():
        if receipt_path.is_file():
            raise BlockStateError("completion receipt exists without its durable guard")
        return None
    state = from_dict(_read_json_object(path)) if path.is_file() else None
    if not guard_path.is_file():
        if receipt_path.is_file():
            raise BlockStateError("completion receipt exists without its durable guard")
        if state is not None and state.state == "COMPLETED" and state.checkpoint_mutation_boundary is None:
            raise BlockStateError("boundary-less COMPLETED state has no durable completion proof")
        return state

    guard = _read_json_object(guard_path)
    try:
        intent = from_dict(guard["intent"])
        transaction_id = guard["transaction_id"]
        completion_sha256 = guard["completion_sha256"]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise BlockStateError(f"malformed completion guard {guard_path}: {exc}") from exc
    if (
        intent.state != "BLOCKED_MANUAL"
        or intent.checkpoint_mutation_boundary is None
        or not isinstance(transaction_id, str)
        or not isinstance(completion_sha256, str)
    ):
        raise BlockStateError(f"invalid completion guard {guard_path}")

    receipt_matches = False
    if receipt_path.is_file():
        receipt = _read_json_object(receipt_path)
        receipt_matches = receipt.get("transaction_id") == transaction_id
    if not receipt_matches:
        return intent
    if state is None:
        return intent
    if state.state != "COMPLETED":
        # A later block may have started after this transaction committed.
        return state
    if hashlib.sha256(_state_payload(state).encode("utf-8")).hexdigest() != completion_sha256:
        return intent
    return state


def _state_payload(state: BlockState) -> str:
    return json.dumps(to_dict(state), indent=2, sort_keys=True) + "\n"


# Fifth remediation (B1): directory-fsync portability contract.
#
# UNSUPPORTED DIRECTORY FSYNC (the only cases allowed to continue, with the
# atomic-replace guarantee alone):
#   1. a non-POSIX platform (`os.name != "posix"`, i.e. Windows), where a
#      directory cannot be opened as a file descriptor for fsync at all;
#   2. on POSIX, `fsync(dirfd)` failing with an errno that POSIX/Linux
#      define as "this descriptor does not support synchronization":
#      EINVAL, ENOTSUP, EOPNOTSUPP.
#
# DIRECTORY FSYNC FAILED (everything else, e.g. EIO, ENOSPC, EBADF,
# EACCES on open, EROFS) raises: the write is reported as failed, so a
# checkpoint mutation intent is never treated as durable.
_DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS: frozenset[int] = frozenset(
    code for code in (errno.EINVAL, getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None))
    if code is not None
)


def _directory_fsync_platform_supported() -> bool:
    return os.name == "posix"


def _fsync_directory(directory: Path) -> bool:
    """fsync the directory entry created by `os.replace`.

    Returns True when the directory was synced, False ONLY for the
    documented UNSUPPORTED cases above. Any other failure (including the
    directory open itself) propagates.
    """
    if not _directory_fsync_platform_supported():
        return False
    dir_fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            if exc.errno in _DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS:
                return False
            raise
    finally:
        os.close(dir_fd)
    return True


def write_block_state(
    ai_runs_dir: Path, state: BlockState, *, require_directory_fsync: bool = False
) -> Path:
    """Atomically write `state` to `.ai-runs/.block-state.json`.

    Durability (fifth remediation, B1): the temporary file is flushed and
    fsync'd, atomically renamed over the real path, and the directory entry
    is fsync'd. A failure in ANY of these steps raises - the caller must
    treat the state as NOT durably recorded (see
    `_DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS` for the only documented
    portability exemption). Note the rename may already be visible when a
    later step fails; callers must never assume the previous record is
    still what is on disk.

    Writes to a temporary file in the same directory (so the eventual
    `os.replace` is an atomic rename on every platform this runner
    supports) and only then replaces the real path — a concurrent reader
    never observes a partially written file, and a crash mid-write leaves
    the previous, still-valid record (or nothing) rather than a corrupt
    partial one. The temporary file is removed on any failure before the
    replace happens.
    """
    ai_runs_dir.mkdir(parents=True, exist_ok=True)
    path = _path(ai_runs_dir)
    payload = _state_payload(state)

    return _write_atomic(
        ai_runs_dir, path, payload,
        sync_directory=True, require_directory_fsync=require_directory_fsync,
    )


def _write_atomic(
    ai_runs_dir: Path, path: Path, payload: str, *, sync_directory: bool,
    require_directory_fsync: bool = False,
) -> Path:
    """Write one complete file; no operation after replace can fail when
    `sync_directory` is false (used only for a receipt whose visibility is
    evidence of an already-durable final state)."""

    fd, tmp_name = tempfile.mkstemp(dir=str(ai_runs_dir), prefix=".block-state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        if sync_directory:
            synced = _fsync_directory(ai_runs_dir)
            if require_directory_fsync and not synced:
                raise BlockStateError(f"directory fsync unsupported for completion publication: {ai_runs_dir}")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def publish_completed_block_state(ai_runs_dir: Path, completed: BlockState) -> Path:
    """Publish checkpoint completion with a durable guard and a receipt.

    The pre-mutation marker remains authoritative in a separate, durably
    installed guard before the boundary-less state can replace it. A receipt
    is made visible *only after* the final state replacement and directory
    fsync have succeeded. The receipt needs no second durability proof: if
    visible, it proves the preceding final-state sync; if lost in a crash,
    the durable guard keeps the workflow blocked. This terminates the
    publication protocol without a recursively committed marker.
    """
    if completed.state != "COMPLETED" or completed.checkpoint_mutation_boundary is not None:
        raise BlockStateError("completion publication requires COMPLETED with no mutation boundary")
    intent = load_block_state(ai_runs_dir)
    if intent is None or intent.state != "BLOCKED_MANUAL" or intent.checkpoint_mutation_boundary is None:
        raise BlockStateError("completion publication requires the durable checkpoint mutation marker")

    transaction_id = uuid.uuid4().hex
    guard = {
        "transaction_id": transaction_id,
        "completion_sha256": hashlib.sha256(_state_payload(completed).encode("utf-8")).hexdigest(),
        "intent": to_dict(intent),
    }
    _write_atomic(
        ai_runs_dir,
        ai_runs_dir / COMPLETION_GUARD_FILENAME,
        json.dumps(guard, indent=2, sort_keys=True) + "\n",
        sync_directory=True,
        require_directory_fsync=True,
    )
    path = write_block_state(ai_runs_dir, completed, require_directory_fsync=True)
    # There is deliberately no operation that can report failure after this
    # replacement: receipt visibility is sufficient because the final state
    # was already durably synced before this call began.
    _write_atomic(
        ai_runs_dir,
        ai_runs_dir / COMPLETION_RECEIPT_FILENAME,
        json.dumps({"transaction_id": transaction_id}, sort_keys=True) + "\n",
        sync_directory=False,
    )
    return path


def new_block_state(
    *,
    branch_name: str,
    first_task: str,
    last_task: str,
    block_name: str,
    tasks_md_path: str | None = None,
    expected_head_oid: str | None = None,
) -> BlockState:
    """The initial record `start-block` writes (T037) — `state: RUNNING`,
    `last_safe_stage: BRANCH_CREATED`, per `contracts/cli-interface.md`'s
    documented `start-block` sequence. Provided here as the one place
    that shape is defined, so `start-block` does not need to hand-
    construct it.

    `tasks_md_path` is the block's resolved Spec Kit feature identity
    (A5) — `start-block` resolves it once (failing closed on ambiguity)
    and this function simply persists whatever it was given.

    `expected_head_oid` (item 6) is `HEAD` immediately after branch
    creation - `resume` verifies against it before proceeding from any
    stage.
    """
    return BlockState(
        branch_name=branch_name,
        first_task=first_task,
        last_task=last_task,
        block_name=block_name,
        state="RUNNING",
        last_safe_stage="BRANCH_CREATED",
        retry_count_current_stage=0,
        last_gate=None,
        updated_at=_utc_now_iso(),
        tasks_md_path=tasks_md_path,
        expected_head_oid=expected_head_oid,
    )


# --- History-based regeneration (research.md §0.7's "Fallback / kept simple") ---
#
# **Corrected per the T022-T032 Codex re-gate (Finding 2 — MAJOR).** A
# prior version of this function tried to recover `last_safe_stage`
# beyond `BRANCH_CREATED` by parsing an invented
# `Scope: T{first}-T{last}: {block_name}` convention out of
# `.ai-runs/*-result.md` headers and matching records to the current
# block by `first_task` alone. Codex independently reproduced several
# ways that was unsafe:
#
# - a record from an unrelated, earlier block sharing only `first_task`
#   could be adopted as if it were this block's own history;
# - `RESULT: PASS` appearing anywhere in a record's free-form prose (e.g.
#   text merely illustrating the contract's shape) was accepted as if it
#   were an actual trailing gate verdict;
# - a Claude (`Tool: claude`) record could be mistaken for a Codex gate
#   result merely because its content happened to contain a `RESULT:`
#   line;
# - an earlier `RESULT: PASS` could survive in the regenerated
#   `last_safe_stage` even after a later, more recent `RESULT: FAIL` for
#   the same (apparently) matching scope.
#
# **Root cause**: no contract closed as of T004-T021/T022-T032 defines a
# field that safely binds a `.ai-runs/*-result.md` record to an EXACT
# Development Block (its specific branch/task-range). `runs/audit.py`'s
# `render_metadata_header` (T016, already closed) DOES authoritatively
# define `Tool` (`"claude"`/`"codex"`, safe to key on) and the
# `STATUS:`/`RESULT:` trailing-marker shapes are themselves authoritative
# (research.md §9/§10, `contracts/codex-gate-result-contract.md`) — but
# `Scope` is caller-supplied free text with no fixed, parseable shape, and
# the header carries no branch-name field at all. Absent that missing
# field, ANY attempt to decide "does this historical record belong to the
# branch I have checked out right now" is an invented, unauthoritative
# guess — exactly the "new cross-component protocol" this remediation was
# told not to invent.
#
# **Resolution**: regeneration recovers ONLY what current Git state alone
# already proves — that a branch named `T{first}-{PascalCase}` is checked
# out — and deliberately does not inspect `.ai-runs/*-result.md` content
# to advance `last_safe_stage` beyond `BRANCH_CREATED` at all. This is a
# real capability loss relative to the removed heuristic, reported here
# rather than solved by re-inventing a narrower version of the same
# unauthoritative protocol: a future contract (T033+) would need to add a
# structured, unambiguous block-identity field to the audit record header
# (e.g. the exact `branch_name`) before progress beyond `BRANCH_CREATED`
# can ever be safely regenerated from audit history. "Recovery may
# under-estimate progress, but must never over-estimate it."


def regenerate_block_state(ai_runs_dir: Path, repo: GitRepo) -> BlockState | None:
    """Reconstruct a best-effort `BlockState` from current Git state, for
    use when the JSON file is missing or a caller (`resume`/`status`,
    T046/T050) has decided it looks stale.

    `ai_runs_dir` is accepted (and kept part of this function's public
    signature) for forward compatibility with a future contract that
    defines a safe block-identity field in the audit record header — see
    the module-level comment above for why it is not yet used to
    recover anything beyond what Git state alone already proves.

    Returns `None` when the currently checked-out branch does not even
    match the `T{first}-{PascalCase}` naming convention (`git/branch.py`)
    — there is then no Development Block to regenerate a record for at
    all, which is a fact this function can state outright rather than
    fabricating one.

    `last_task` and `block_name` are NOT recoverable from Git state
    alone either (`PascalCase` block-name derivation is lossy and never
    encodes the task range) — `last_task` conservatively falls back to
    `first_task`, and `block_name` falls back to the branch's own
    PascalCase segment rather than any claimed-recovered original free
    text. Both are best-effort placeholders, not reconstructed originals.

    The returned record's `state` is always `"BLOCKED_MANUAL"` — a
    regenerated record represents a *lost* persistent cache being rebuilt
    from what little is provable, never an actively `RUNNING` stage
    (nothing is provably running when this is called) and never
    `"READY_TO_RESUME"` (data-model.md/research.md §0.7 reserve that
    value exclusively for the `resume` command's own successful
    revalidation, never for this read-only reconstruction primitive).
    This is the conservative, fail-closed choice consistent with "no
    autonomous continuation": a regenerated record always requires the
    normal `resume` revalidation gate before anything continues from it.
    """
    del ai_runs_dir  # not yet safely usable — see the module-level comment above

    branch_name = repo.current_branch()
    parsed = parse_branch_name(branch_name)
    if parsed is None:
        return None
    first_task_number, pascal_block_name = parsed
    first_task = f"T{first_task_number}"

    return BlockState(
        branch_name=branch_name,
        first_task=first_task,
        last_task=first_task,
        block_name=pascal_block_name,
        state="BLOCKED_MANUAL",
        last_safe_stage="BRANCH_CREATED",
        retry_count_current_stage=0,
        last_gate=None,
        updated_at=_utc_now_iso(),
    )
