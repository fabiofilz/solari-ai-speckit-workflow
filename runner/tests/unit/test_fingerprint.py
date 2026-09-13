"""Unit tests for `fingerprint/engine.py` (T029/T030, research.md §12).

`compute_fingerprint` tests use a real, disposable temporary Git
repository (`tmp_path` + real `git init`) exactly like `test_git_ops.py`/
`test_candidate_tree.py` — never the real project repository.
`verify_fingerprint`/`derived_label` tests construct `Fingerprint` records
directly, since their behavior does not depend on Git at all.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from solari_workflow.fingerprint.engine import (
    Fingerprint,
    compute_fingerprint,
    derived_label,
    fingerprint_from_dict,
    fingerprint_identity,
    fingerprint_to_dict,
    verify_fingerprint,
)
from solari_workflow.git import ops
from solari_workflow.git.candidate_tree import build_candidate_tree

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "tracked.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)


_BASE = Fingerprint(
    head_oid="a" * 40,
    candidate_tree_oid="b" * 40,
    branch_name="T001-Sample",
    first_task="T001",
    last_task="T003",
    computed_at="2026-01-01T00:00:00Z",
)


# --- compute_fingerprint --------------------------------------------------


def test_compute_fingerprint_binds_candidate_tree_oids_and_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    candidate = build_candidate_tree(repo)

    fp = compute_fingerprint(repo, candidate, first_task="T001", last_task="T003")

    assert fp.head_oid == candidate.head_oid
    assert fp.candidate_tree_oid == candidate.tree_oid
    assert fp.branch_name == "main"
    assert fp.first_task == "T001"
    assert fp.last_task == "T003"
    assert fp.computed_at  # non-empty UTC timestamp


def test_compute_fingerprint_is_deterministic_for_unchanged_state(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    candidate = build_candidate_tree(repo)

    first = compute_fingerprint(repo, candidate, first_task="T001", last_task="T003")
    second = compute_fingerprint(repo, candidate, first_task="T001", last_task="T003")

    assert first.head_oid == second.head_oid
    assert first.candidate_tree_oid == second.candidate_tree_oid


# --- verify_fingerprint: strict, full-block-identity equality -------------
#
# Corrected per the T022-T032 Codex re-gate (Finding 1 — MAJOR): the
# approved minimum fingerprint identity is `head_oid`, `candidate_tree_oid`,
# `branch_name`, AND the task range (`first_task`, `last_task`) — not just
# the two OIDs. `computed_at` remains pure metadata and never participates.


def test_verify_fingerprint_true_for_identical_records() -> None:
    assert verify_fingerprint(_BASE, _BASE) is True


def test_verify_fingerprint_true_for_identical_identity_with_a_different_computed_at() -> None:
    """`computed_at` is metadata only — it must never affect authoritative
    identity comparison, in either direction."""
    other = replace(_BASE, computed_at="2030-01-01T00:00:00Z")
    assert verify_fingerprint(_BASE, other) is True


def test_verify_fingerprint_false_when_only_head_oid_differs() -> None:
    other = replace(_BASE, head_oid="c" * 40)
    assert verify_fingerprint(_BASE, other) is False


def test_verify_fingerprint_false_when_only_candidate_tree_oid_differs() -> None:
    other = replace(_BASE, candidate_tree_oid="c" * 40)
    assert verify_fingerprint(_BASE, other) is False


def test_verify_fingerprint_false_when_both_oids_differ() -> None:
    other = replace(_BASE, head_oid="c" * 40, candidate_tree_oid="d" * 40)
    assert verify_fingerprint(_BASE, other) is False


def test_verify_fingerprint_false_when_only_branch_name_differs() -> None:
    """A Fingerprint bound to a different branch must never verify equal,
    even with identical OIDs — this is exactly the T022-T032 re-gate's
    Finding 1: the prior verifier ignored `branch_name` entirely."""
    other = replace(_BASE, branch_name="T099-DifferentBlock")
    assert verify_fingerprint(_BASE, other) is False


def test_verify_fingerprint_false_when_only_first_task_differs() -> None:
    other = replace(_BASE, first_task="T002")
    assert verify_fingerprint(_BASE, other) is False


def test_verify_fingerprint_false_when_only_last_task_differs() -> None:
    other = replace(_BASE, last_task="T004")
    assert verify_fingerprint(_BASE, other) is False


def test_fingerprint_identity_exposes_the_explicit_five_field_tuple() -> None:
    """The explicit semantic API a future checkpoint/resume revalidation
    (T040/T046) should reuse directly, rather than re-deriving its own
    notion of "identity" from `Fingerprint`'s raw fields."""
    identity = fingerprint_identity(_BASE)
    assert identity == (
        _BASE.head_oid,
        _BASE.candidate_tree_oid,
        _BASE.branch_name,
        _BASE.first_task,
        _BASE.last_task,
    )
    assert _BASE.computed_at not in identity


def test_fingerprint_identity_excludes_computed_at_from_equality() -> None:
    other = replace(_BASE, computed_at="2030-01-01T00:00:00Z")
    assert fingerprint_identity(_BASE) == fingerprint_identity(other)


def test_native_dataclass_equality_is_not_relied_upon_for_identity() -> None:
    """Regression guard: native dataclass `__eq__` includes `computed_at`,
    so it must never be used as a stand-in for authoritative identity
    comparison — `verify_fingerprint` is the only correct API for that."""
    other = replace(_BASE, computed_at="2030-01-01T00:00:00Z")
    assert _BASE != other  # dataclass equality DOES differ here...
    assert verify_fingerprint(_BASE, other) is True  # ...but identity does not


# --- derived_label: stable, non-authoritative -----------------------------


def test_derived_label_is_stable_for_identical_inputs() -> None:
    assert derived_label(_BASE) == derived_label(_BASE)


def test_derived_label_changes_when_either_oid_changes() -> None:
    baseline = derived_label(_BASE)
    assert derived_label(replace(_BASE, head_oid="c" * 40)) != baseline
    assert derived_label(replace(_BASE, candidate_tree_oid="c" * 40)) != baseline


def test_derived_label_is_a_16_character_hex_string() -> None:
    label = derived_label(_BASE)
    assert len(label) == 16
    assert all(c in "0123456789abcdef" for c in label)


# --- (de)serialization ------------------------------------------------------


def test_fingerprint_dict_round_trip() -> None:
    data = fingerprint_to_dict(_BASE)
    restored = fingerprint_from_dict(data)
    assert restored == _BASE


def test_fingerprint_dict_round_trip_preserves_authoritative_identity() -> None:
    """The T022-T032 re-gate's own required scenario: serialization/
    deserialization must preserve the FULL identity, not just the OIDs."""
    data = fingerprint_to_dict(_BASE)
    restored = fingerprint_from_dict(data)
    assert verify_fingerprint(_BASE, restored) is True
    assert fingerprint_identity(_BASE) == fingerprint_identity(restored)


# --- Fail-closed validation of identity fields ------------------------------


def test_fingerprint_rejects_a_malformed_head_oid() -> None:
    with pytest.raises(ValueError):
        replace(_BASE, head_oid="not-a-real-oid")


def test_fingerprint_rejects_a_malformed_candidate_tree_oid() -> None:
    with pytest.raises(ValueError):
        replace(_BASE, candidate_tree_oid="too-short")


def test_fingerprint_rejects_an_empty_branch_name() -> None:
    with pytest.raises(ValueError):
        replace(_BASE, branch_name="")


def test_fingerprint_rejects_a_malformed_first_task() -> None:
    with pytest.raises(ValueError):
        replace(_BASE, first_task="not-a-task-id")


def test_fingerprint_rejects_a_malformed_last_task() -> None:
    with pytest.raises(ValueError):
        replace(_BASE, last_task="091")  # missing the leading "T"


def test_fingerprint_from_dict_raises_on_a_missing_field() -> None:
    data = fingerprint_to_dict(_BASE)
    del data["candidate_tree_oid"]
    with pytest.raises(KeyError):
        fingerprint_from_dict(data)
