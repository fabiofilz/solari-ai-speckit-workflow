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
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solari_workflow.fingerprint.engine import Fingerprint, fingerprint_from_dict, fingerprint_to_dict
from solari_workflow.git.branch import parse_branch_name
from solari_workflow.git.ops import GitRepo

BLOCK_STATE_FILENAME = ".block-state.json"

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
    """Plain-dict serialization, with `last_gate` rendered as a nested
    dict (or `None`) rather than a raw dataclass instance."""
    data = asdict(state)
    data["last_gate"] = None if state.last_gate is None else fingerprint_to_dict(state.last_gate)
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
        )
    except (TypeError, ValueError) as exc:
        raise BlockStateError(f"malformed block-state payload: {exc}") from exc


def load_block_state(ai_runs_dir: Path) -> BlockState | None:
    """Read `.ai-runs/.block-state.json`.

    Returns `None` only for the ordinary, legitimate case of no block ever
    having started (the file does not exist yet) — never for a file that
    exists but cannot be parsed/validated, which always raises
    :class:`BlockStateError` instead (fail-closed: a corrupt record must
    never be silently treated as "no active block")."""
    path = _path(ai_runs_dir)
    if not path.is_file():
        return None
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
    return from_dict(data)


def write_block_state(ai_runs_dir: Path, state: BlockState) -> Path:
    """Atomically write `state` to `.ai-runs/.block-state.json`.

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
    payload = json.dumps(to_dict(state), indent=2, sort_keys=True) + "\n"

    fd, tmp_name = tempfile.mkstemp(dir=str(ai_runs_dir), prefix=".block-state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def new_block_state(
    *,
    branch_name: str,
    first_task: str,
    last_task: str,
    block_name: str,
) -> BlockState:
    """The initial record `start-block` writes (T037, a later phase, not
    implemented here) — `state: RUNNING`, `last_safe_stage: BRANCH_CREATED`,
    per `contracts/cli-interface.md`'s documented `start-block` sequence.
    Provided here as the one place that shape is defined, so a future
    `start-block` implementation does not need to hand-construct it."""
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
