"""Unit tests for `state/block_state.py` (T031/T032, research.md §0.7).

Regeneration tests use a real, disposable temporary Git repository
(`tmp_path` + real `git init`) — never the real project repository.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow.fingerprint.engine import Fingerprint
from solari_workflow.git import ops
from solari_workflow.runs.audit import create_run_record, render_metadata_header, write_result
from solari_workflow.state.block_state import (
    LAST_SAFE_STAGE_VALUES,
    STATE_VALUES,
    BlockState,
    BlockStateError,
    from_dict,
    load_block_state,
    new_block_state,
    regenerate_block_state,
    to_dict,
    write_block_state,
)

_FINGERPRINT = Fingerprint(
    head_oid="a" * 40,
    candidate_tree_oid="b" * 40,
    branch_name="T001-Sample",
    first_task="T001",
    last_task="T003",
    computed_at="2026-01-01T00:00:00Z",
)


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        branch_name="T001-Sample",
        first_task="T001",
        last_task="T003",
        block_name="Sample",
        state="RUNNING",
        last_safe_stage="BRANCH_CREATED",
        retry_count_current_stage=0,
        last_gate=None,
        updated_at="2026-01-01T00:00:00Z",
    )
    base.update(overrides)
    return base


# --- Vocabulary / transition validation ------------------------------------


def test_every_declared_state_value_constructs_successfully() -> None:
    for state in STATE_VALUES:
        BlockState(**_valid_kwargs(state=state))  # must not raise


def test_every_declared_last_safe_stage_value_constructs_successfully() -> None:
    for stage in LAST_SAFE_STAGE_VALUES:
        BlockState(**_valid_kwargs(last_safe_stage=stage))  # must not raise


def test_invalid_state_value_is_rejected() -> None:
    with pytest.raises(BlockStateError):
        BlockState(**_valid_kwargs(state="NOT_A_REAL_STATE"))


def test_invalid_last_safe_stage_value_is_rejected() -> None:
    with pytest.raises(BlockStateError):
        BlockState(**_valid_kwargs(last_safe_stage="NOT_A_REAL_STAGE"))


# --- Fixed single-retry ceiling ---------------------------------------------


def test_retry_count_zero_and_one_are_both_valid() -> None:
    BlockState(**_valid_kwargs(retry_count_current_stage=0))
    BlockState(**_valid_kwargs(retry_count_current_stage=1))


def test_retry_count_never_exceeds_the_fixed_ceiling_of_one() -> None:
    with pytest.raises(BlockStateError):
        BlockState(**_valid_kwargs(retry_count_current_stage=2))


def test_retry_count_rejects_negative_values() -> None:
    with pytest.raises(BlockStateError):
        BlockState(**_valid_kwargs(retry_count_current_stage=-1))


# --- (de)serialization / round-trip -----------------------------------------


def test_to_dict_from_dict_round_trip_without_a_gate() -> None:
    state = BlockState(**_valid_kwargs())
    assert from_dict(to_dict(state)) == state


def test_to_dict_from_dict_round_trip_with_a_gate() -> None:
    state = BlockState(**_valid_kwargs(last_safe_stage="GATE_PASSED", last_gate=_FINGERPRINT))
    restored = from_dict(to_dict(state))
    assert restored == state
    assert restored.last_gate == _FINGERPRINT


def test_from_dict_raises_on_a_missing_required_field() -> None:
    data = to_dict(BlockState(**_valid_kwargs()))
    del data["state"]
    with pytest.raises(BlockStateError):
        from_dict(data)


def test_new_block_state_shape() -> None:
    state = new_block_state(
        branch_name="T091-ValidationPipeline",
        first_task="T091",
        last_task="T099",
        block_name="Validation Pipeline",
    )
    assert state.state == "RUNNING"
    assert state.last_safe_stage == "BRANCH_CREATED"
    assert state.retry_count_current_stage == 0
    assert state.last_gate is None


# --- load/write against a real filesystem -----------------------------------


def test_load_block_state_returns_none_when_no_file_exists(tmp_path: Path) -> None:
    assert load_block_state(tmp_path) is None


def test_write_then_load_round_trips(tmp_path: Path) -> None:
    state = BlockState(**_valid_kwargs(last_safe_stage="GATE_PASSED", last_gate=_FINGERPRINT))
    path = write_block_state(tmp_path, state)
    assert path.is_file()
    loaded = load_block_state(tmp_path)
    assert loaded == state


def test_write_block_state_creates_the_ai_runs_directory_if_missing(tmp_path: Path) -> None:
    ai_runs_dir = tmp_path / ".ai-runs"
    assert not ai_runs_dir.exists()
    write_block_state(ai_runs_dir, BlockState(**_valid_kwargs()))
    assert ai_runs_dir.is_dir()


def test_write_block_state_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    write_block_state(tmp_path, BlockState(**_valid_kwargs()))
    remaining = list(tmp_path.iterdir())
    assert [p.name for p in remaining] == [".block-state.json"]


def test_load_block_state_raises_on_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / ".block-state.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(BlockStateError):
        load_block_state(tmp_path)


def test_load_block_state_raises_when_json_is_not_an_object(tmp_path: Path) -> None:
    (tmp_path / ".block-state.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(BlockStateError):
        load_block_state(tmp_path)


def test_load_block_state_raises_on_an_invalid_state_value_on_disk(tmp_path: Path) -> None:
    data = to_dict(BlockState(**_valid_kwargs()))
    data["state"] = "NOT_A_REAL_STATE"
    (tmp_path / ".block-state.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BlockStateError):
        load_block_state(tmp_path)


# --- retry_count_current_stage: strict JSON-domain validation (T022-T032 --
# re-gate, Finding 4 — MINOR). `int(value)` coercion previously accepted
# `true` -> `1` and `0.5` -> `0`; persisted `retry_count_current_stage`
# must be an actual JSON integer in {0, 1} — never a coerced bool/float/
# string/null/out-of-range value.


@pytest.mark.parametrize("malformed_value", [True, False, 0.5, 1.0, "0", "1", None, -1, 2])
def test_load_block_state_rejects_a_malformed_retry_count_on_disk(tmp_path: Path, malformed_value: object) -> None:
    data = to_dict(BlockState(**_valid_kwargs()))
    data["retry_count_current_stage"] = malformed_value
    (tmp_path / ".block-state.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BlockStateError):
        load_block_state(tmp_path)


def test_from_dict_rejects_a_boolean_retry_count_even_though_bool_is_an_int_subclass() -> None:
    """The specific Codex-reproduced coercion: `True`/`False` are `int`
    subclasses in Python, so a naive `int(value)`/`isinstance(value, int)`
    check alone would silently accept them as `1`/`0`."""
    data = to_dict(BlockState(**_valid_kwargs()))
    data["retry_count_current_stage"] = True
    with pytest.raises(BlockStateError):
        from_dict(data)


def test_from_dict_rejects_a_float_retry_count() -> None:
    data = to_dict(BlockState(**_valid_kwargs()))
    data["retry_count_current_stage"] = 0.5
    with pytest.raises(BlockStateError):
        from_dict(data)


def test_from_dict_accepts_real_integer_retry_counts() -> None:
    for valid_value in (0, 1):
        data = to_dict(BlockState(**_valid_kwargs()))
        data["retry_count_current_stage"] = valid_value
        assert from_dict(data).retry_count_current_stage == valid_value


def test_direct_completed_write_without_publication_proof_is_not_authoritative(tmp_path: Path) -> None:
    write_block_state(tmp_path, BlockState(**_valid_kwargs(state="RUNNING")))
    write_block_state(tmp_path, BlockState(**_valid_kwargs(state="COMPLETED")))
    with pytest.raises(BlockStateError, match="no durable completion proof"):
        load_block_state(tmp_path)


# --- Regeneration from Git state (T022-T032 re-gate, Finding 2 — MAJOR) ----
#
# Corrected: a prior version of `regenerate_block_state` tried to recover
# `last_safe_stage` beyond `BRANCH_CREATED` by parsing an invented
# `Scope: T{first}-T{last}: {block_name}` convention out of
# `.ai-runs/*-result.md` headers. Codex reproduced several ways that was
# unsafe (cross-block matches on `first_task` alone, prose containing
# "RESULT: PASS" being accepted, a Claude record being mistaken for a
# Codex gate result, and a later FAIL not rolling back an earlier PASS).
# No currently-closed contract (T004-T021, T022-T032) defines a field
# that safely binds an audit record to an EXACT Development Block, so
# regeneration is now deliberately conservative: it recovers only what
# Git state alone can prove (`branch_name`/`first_task`, and that a
# properly named branch exists at all), and NEVER inspects
# `.ai-runs/*-result.md` content to advance `last_safe_stage` beyond
# `BRANCH_CREATED` — "recovery may under-estimate progress, but must
# never over-estimate it."
#
# Only this section needs real `git` — a global `pytestmark` would wrongly
# skip the pure vocabulary/serialization tests above whenever `git` is
# absent from `PATH`, so the skip is applied per-test instead.

_requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _init_repo_on_block_branch(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)
    subprocess.run(["git", "-C", str(path), "switch", "-c", "T091-ValidationPipeline"], check=True)


def _write_result_record(
    ai_runs_dir: Path, *, tool: str = "claude", scope: str, body_marker: str
) -> None:
    record = create_run_record(ai_runs_dir, slug_source=scope, prompt_content="prompt\n")
    header = render_metadata_header(
        tool=tool,
        model="fake",
        effort="fake",
        session_mode="NEW",
        prior_session_id=None,
        speckit_involved=False,
        scope=scope,
        purpose="test",
        workflow_schema_version="1",
        workflow_version="0.1.0",
    )
    write_result(record, header + "\n" + body_marker + "\n")


@_requires_git
def test_regenerate_returns_none_for_a_branch_not_matching_the_block_naming_convention(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "initial"], check=True)

    repo = ops.open_repository(tmp_path)
    assert regenerate_block_state(tmp_path / ".ai-runs", repo) is None


@_requires_git
def test_regenerate_with_no_audit_records_falls_back_to_branch_created(tmp_path: Path) -> None:
    _init_repo_on_block_branch(tmp_path)
    repo = ops.open_repository(tmp_path)
    ai_runs_dir = tmp_path / ".ai-runs"

    state = regenerate_block_state(ai_runs_dir, repo)

    assert state is not None
    assert state.branch_name == "T091-ValidationPipeline"
    assert state.first_task == "T091"
    assert state.last_task == "T091"  # not recoverable from Git state alone
    assert state.last_safe_stage == "BRANCH_CREATED"
    assert state.state == "BLOCKED_MANUAL"
    assert state.last_gate is None


@_requires_git
def test_regenerate_never_advances_past_branch_created_even_with_a_matching_status_complete_record(
    tmp_path: Path,
) -> None:
    """A same-first-task, well-formed, trailing `STATUS: COMPLETE` Claude
    record is exactly the kind of evidence a less conservative design
    would have trusted — it must still never advance the regenerated
    stage, since no authoritative field binds it to this exact block."""
    _init_repo_on_block_branch(tmp_path)
    ai_runs_dir = tmp_path / ".ai-runs"
    _write_result_record(
        ai_runs_dir, tool="claude", scope="T091-T099: Validation Pipeline", body_marker="STATUS: COMPLETE"
    )

    repo = ops.open_repository(tmp_path)
    state = regenerate_block_state(ai_runs_dir, repo)

    assert state is not None
    assert state.last_safe_stage == "BRANCH_CREATED"


@_requires_git
def test_regenerate_never_advances_past_branch_created_even_with_a_matching_result_pass_record(
    tmp_path: Path,
) -> None:
    """Same as above for a well-formed, trailing Codex `RESULT: PASS`."""
    _init_repo_on_block_branch(tmp_path)
    ai_runs_dir = tmp_path / ".ai-runs"
    _write_result_record(
        ai_runs_dir,
        tool="codex",
        scope="T091-T099: Validation Pipeline",
        body_marker="RESULT: PASS\nFINDINGS: []",
    )

    repo = ops.open_repository(tmp_path)
    state = regenerate_block_state(ai_runs_dir, repo)

    assert state is not None
    assert state.last_safe_stage == "BRANCH_CREATED"


@_requires_git
def test_regenerate_ignores_a_same_first_task_different_last_task_record(tmp_path: Path) -> None:
    """Reproduces the exact cross-block collision Codex found: a record
    sharing only `first_task` (T091) with a DIFFERENT `last_task`/
    `block_name` (an abandoned or unrelated block) must never be treated
    as evidence for the currently checked-out block."""
    _init_repo_on_block_branch(tmp_path)
    ai_runs_dir = tmp_path / ".ai-runs"
    _write_result_record(
        ai_runs_dir,
        tool="codex",
        scope="T091-T095: An Abandoned Earlier Block",
        body_marker="RESULT: PASS\nFINDINGS: []",
    )

    repo = ops.open_repository(tmp_path)
    state = regenerate_block_state(ai_runs_dir, repo)

    assert state is not None
    assert state.last_task == "T091"  # never adopted "T095" from the unrelated record
    assert state.last_safe_stage == "BRANCH_CREATED"


@_requires_git
def test_regenerate_does_not_mistake_a_claude_record_for_a_codex_gate_result(tmp_path: Path) -> None:
    """A Claude (`Tool: claude`) record whose free-form content happens to
    contain the literal text `RESULT: PASS` must never be counted as
    Codex gate evidence."""
    _init_repo_on_block_branch(tmp_path)
    ai_runs_dir = tmp_path / ".ai-runs"
    _write_result_record(
        ai_runs_dir,
        tool="claude",
        scope="T091-T099: Validation Pipeline",
        body_marker="STATUS: COMPLETE\nRESULT: PASS\nFINDINGS: []",
    )

    repo = ops.open_repository(tmp_path)
    state = regenerate_block_state(ai_runs_dir, repo)

    assert state is not None
    assert state.last_safe_stage == "BRANCH_CREATED"


@_requires_git
def test_regenerate_ignores_arbitrary_prose_containing_pass(tmp_path: Path) -> None:
    _init_repo_on_block_branch(tmp_path)
    ai_runs_dir = tmp_path / ".ai-runs"
    _write_result_record(
        ai_runs_dir,
        tool="codex",
        scope="T091-T099: Validation Pipeline",
        body_marker=(
            "The contract requires a trailing block shaped like:\n"
            "RESULT: PASS\n"
            "FINDINGS: []\n"
            "...but this text is merely illustrating the shape, not the actual verdict.\n"
        ),
    )

    repo = ops.open_repository(tmp_path)
    state = regenerate_block_state(ai_runs_dir, repo)

    assert state is not None
    assert state.last_safe_stage == "BRANCH_CREATED"


@_requires_git
def test_regenerate_does_not_recover_gate_passed_even_when_an_earlier_pass_is_followed_by_a_later_fail(
    tmp_path: Path,
) -> None:
    _init_repo_on_block_branch(tmp_path)
    ai_runs_dir = tmp_path / ".ai-runs"
    _write_result_record(
        ai_runs_dir, tool="codex", scope="T091-T099: Validation Pipeline", body_marker="RESULT: PASS\nFINDINGS: []"
    )
    _write_result_record(
        ai_runs_dir,
        tool="codex",
        scope="T091-T099: Validation Pipeline",
        body_marker="RESULT: FAIL\nFINDINGS:\n- severity: major\n  summary: regression",
    )

    repo = ops.open_repository(tmp_path)
    state = regenerate_block_state(ai_runs_dir, repo)

    assert state is not None
    assert state.last_safe_stage == "BRANCH_CREATED"


@_requires_git
def test_regenerate_always_returns_blocked_manual_regardless_of_audit_content(tmp_path: Path) -> None:
    """No autonomous resume or checkpoint authority: a regenerated record
    is never `RUNNING`, `READY_TO_RESUME`, or `COMPLETED` — only
    `BLOCKED_MANUAL`, which always requires the normal `resume`
    revalidation gate (a later phase, not implemented here) before
    anything continues."""
    _init_repo_on_block_branch(tmp_path)
    ai_runs_dir = tmp_path / ".ai-runs"
    _write_result_record(
        ai_runs_dir, tool="codex", scope="T091-T099: Validation Pipeline", body_marker="RESULT: PASS\nFINDINGS: []"
    )

    repo = ops.open_repository(tmp_path)
    state = regenerate_block_state(ai_runs_dir, repo)

    assert state is not None
    assert state.state == "BLOCKED_MANUAL"
    assert state.retry_count_current_stage == 0
    assert state.last_gate is None
