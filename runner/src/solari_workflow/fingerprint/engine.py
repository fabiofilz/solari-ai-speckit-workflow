"""Fingerprint record: binds a Development Block to its Candidate Tree
(data-model.md's "Fingerprint (Candidate Tree binding)", research.md §12).

A Fingerprint is a **structured record**, not a single opaque hash — the
Git object OIDs it is built from are already collision-resistant, so
wrapping them in a further hash would add nothing to the authoritative
equality check. Equality — for both the before/after-Codex technical
backstop (research.md §10/§12) and the pre-checkpoint recheck (research.md
§13, `checkpoint`/`resume`, T040/T046 — a later phase, not implemented
here) — always means BOTH `head_oid` AND `candidate_tree_oid` match, never
either alone and never a derived composite hash.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from solari_workflow.git.candidate_tree import CandidateTree
from solari_workflow.git.ops import GitRepo

_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_TASK_ID_RE = re.compile(r"^T\d+$")

# The authoritative fingerprint identity — corrected per the T022-T032
# Codex re-gate (Finding 1 — MAJOR). `computed_at` is deliberately
# excluded: it is metadata recording *when* a Fingerprint was computed,
# never a component of *what* it identifies.
FingerprintIdentity = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class Fingerprint:
    """`{head_oid, candidate_tree_oid, branch_name, first_task, last_task,
    computed_at}` (data-model.md) — every field a plain string except
    `computed_at`, itself an ISO-8601 UTC string for trivial JSON
    round-tripping (`.ai-runs/.block-state.json`'s `last_gate` field,
    `state/block_state.py`).

    Validated on construction: `head_oid`/`candidate_tree_oid` must be
    well-formed 40-character hex Git OIDs, `branch_name` must be
    non-empty, and `first_task`/`last_task` must match the `T<digits>`
    task-id shape (`git/branch.py`'s own convention) — a malformed
    identity field fails closed at construction time rather than
    silently becoming part of a Fingerprint nothing can then trust.
    """

    head_oid: str
    candidate_tree_oid: str
    branch_name: str
    first_task: str
    last_task: str
    computed_at: str

    def __post_init__(self) -> None:
        if not _OID_RE.match(self.head_oid):
            raise ValueError(
                f"invalid head_oid {self.head_oid!r}; expected a 40-character hex Git OID"
            )
        if not _OID_RE.match(self.candidate_tree_oid):
            raise ValueError(
                f"invalid candidate_tree_oid {self.candidate_tree_oid!r}; "
                "expected a 40-character hex Git OID"
            )
        if not self.branch_name:
            raise ValueError("branch_name must be non-empty")
        if not _TASK_ID_RE.match(self.first_task):
            raise ValueError(f"invalid first_task {self.first_task!r}; expected shape 'T<digits>'")
        if not _TASK_ID_RE.match(self.last_task):
            raise ValueError(f"invalid last_task {self.last_task!r}; expected shape 'T<digits>'")
        if not self.computed_at:
            raise ValueError("computed_at must be non-empty")


def compute_fingerprint(
    repo: GitRepo,
    candidate: CandidateTree,
    *,
    first_task: str,
    last_task: str,
) -> Fingerprint:
    """Compute Fingerprint X from an already-built Candidate Tree.

    `candidate.head_oid` (captured once, at Candidate Tree build time) is
    reused directly here rather than re-querying `HEAD` a second time, so
    a Fingerprint can never observe a `head_oid` that drifted from the
    exact Candidate Tree it is being bound to.
    """
    return Fingerprint(
        head_oid=candidate.head_oid,
        candidate_tree_oid=candidate.tree_oid,
        branch_name=repo.current_branch(),
        first_task=first_task,
        last_task=last_task,
        computed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def fingerprint_identity(fingerprint: Fingerprint) -> FingerprintIdentity:
    """The explicit, authoritative identity tuple — `(head_oid,
    candidate_tree_oid, branch_name, first_task, last_task)` — corrected
    per the T022-T032 Codex re-gate (Finding 1 — MAJOR): the approved
    minimum fingerprint identity binds the whole Development Block, not
    just its two Git OIDs. `computed_at` is never included: it is
    metadata about *when* a Fingerprint was computed, not part of *what*
    it identifies, and must never affect an identity comparison in either
    direction.

    Exposed as its own function — rather than folding the comparison
    directly into `verify_fingerprint` — so a future checkpoint/resume
    revalidation (`git/checkpoint.py`/`state/block_state.py`'s `resume`
    composition, T040/T046, a later phase not implemented here) can reuse
    the exact same authoritative identity definition without re-deriving
    it, and so there is exactly one place this identity is defined.

    Never use `Fingerprint`'s native dataclass equality (`==`) as a
    substitute for this: dataclass equality also compares `computed_at`,
    which two otherwise-identical Fingerprints legitimately differ on.
    """
    return (
        fingerprint.head_oid,
        fingerprint.candidate_tree_oid,
        fingerprint.branch_name,
        fingerprint.first_task,
        fingerprint.last_task,
    )


def verify_fingerprint(expected: Fingerprint, actual: Fingerprint) -> bool:
    """Strict equality over the full authoritative identity — `head_oid`,
    `candidate_tree_oid`, `branch_name`, `first_task`, AND `last_task`
    must all match (see :func:`fingerprint_identity`). Never a
    single-field or two-field-only comparison, and never `computed_at`,
    which is metadata only.
    """
    return fingerprint_identity(expected) == fingerprint_identity(actual)


def derived_label(fingerprint: Fingerprint) -> str:
    """Short derived id (`sha256(f"{head_oid}:{candidate_tree_oid}")[:16]`).

    Purely a compact label for filenames/logs (research.md §12) — never
    used for the authoritative equality check, which always compares
    `head_oid` and `candidate_tree_oid` directly via `verify_fingerprint`.
    """
    digest = hashlib.sha256(
        f"{fingerprint.head_oid}:{fingerprint.candidate_tree_oid}".encode("utf-8")
    ).hexdigest()
    return digest[:16]


def fingerprint_to_dict(fingerprint: Fingerprint) -> dict[str, str]:
    """Plain-dict serialization for embedding in JSON (`.ai-runs/.block-state.json`)
    or Markdown (`.ai-runs/*-result.md`)."""
    return asdict(fingerprint)


def fingerprint_from_dict(data: dict[str, Any]) -> Fingerprint:
    """Inverse of :func:`fingerprint_to_dict`.

    Every field is coerced to `str` and every required key is accessed
    directly (a `KeyError` on a missing field propagates as-is) — callers
    that need a friendlier, aggregated error message for a malformed
    on-disk payload (e.g. `state/block_state.py`'s block-state loader)
    wrap this call themselves rather than this module inventing its own
    error type for what is, at this level, simply a dict shape mismatch.
    """
    return Fingerprint(
        head_oid=str(data["head_oid"]),
        candidate_tree_oid=str(data["candidate_tree_oid"]),
        branch_name=str(data["branch_name"]),
        first_task=str(data["first_task"]),
        last_task=str(data["last_task"]),
        computed_at=str(data["computed_at"]),
    )
