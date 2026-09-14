"""Regression tests for B1 (a new gate ATTEMPT always supersedes an
earlier one for the same block) - the exact scenario named by the gate
finding:

    PASS -> no tree change -> later FAIL -> checkpoint MUST be refused

plus the analogous cases for a malformed/unparseable re-gate and a
drift-detected re-gate, each of which must also revoke an earlier
eligible PASS rather than leaving it sitting in `last_gate` for
`checkpoint` to (mis)trust.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _start_and_implement(project_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0
    (project_root / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0


def test_pass_then_later_fail_with_no_tree_change_revokes_checkpoint_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact B1 scenario: PASS -> no tree change -> later FAIL ->
    checkpoint MUST be refused."""
    project_root = init_repo_with_config(tmp_path)
    _start_and_implement(project_root, monkeypatch)
    ai_runs_dir = project_root / ".ai-runs"

    # First gate: PASS.
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    state = load_block_state(ai_runs_dir)
    assert state.last_safe_stage == "GATE_PASSED"
    assert state.last_gate is not None

    # Second gate, run again with NO tree change in between: FAIL. A
    # buggy implementation would leave the first PASS's Fingerprint
    # sitting in `last_gate`, still matching the (unchanged) working
    # tree, and `checkpoint` would then wrongly succeed against a
    # SUPERSEDED verdict.
    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "FAIL", "FAKE_CODEX_FINDINGS": "blocker:found on second look"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 1

    state = load_block_state(ai_runs_dir)
    assert state.state == "QA_REMEDIATION_REQUIRED"
    assert state.last_gate is None  # the earlier PASS was revoked
    assert state.last_safe_stage == "IMPLEMENTATION_COMPLETE"  # downgraded from GATE_PASSED

    # checkpoint MUST be refused.
    exit_code = run_cli(["checkpoint", "--project-root", str(project_root)])
    assert exit_code == 1
    state = load_block_state(ai_runs_dir)
    assert state.state != "COMPLETED"


def test_pass_then_later_malformed_result_revokes_checkpoint_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = init_repo_with_config(tmp_path)
    _start_and_implement(project_root, monkeypatch)
    ai_runs_dir = project_root / ".ai-runs"

    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert load_block_state(ai_runs_dir).last_gate is not None

    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "MALFORMED"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 1
    state = load_block_state(ai_runs_dir)
    assert state.last_gate is None
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 1


def test_pass_then_later_ineligible_pass_revokes_checkpoint_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `RESULT: PASS` with a blocking-severity finding is not eligible -
    and must ALSO revoke an earlier, genuinely eligible PASS."""
    project_root = init_repo_with_config(tmp_path)
    _start_and_implement(project_root, monkeypatch)
    ai_runs_dir = project_root / ".ai-runs"

    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert load_block_state(ai_runs_dir).last_gate is not None

    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS", "FAKE_CODEX_FINDINGS": "blocker:actually not fine"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 1
    state = load_block_state(ai_runs_dir)
    assert state.last_gate is None
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 1


def test_pass_then_later_operational_failure_leaves_block_manual_and_checkpoint_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1's exact second-round scenario: PASS -> later actor/gate failure
    -> BLOCKED_MANUAL -> checkpoint MUST refuse. A `BLOCKED_MANUAL` block
    is never checkpoint-eligible, even if stale PASS data somehow
    remained cached (it does not here, thanks to `_revoke_prior_gate` -
    but `checkpoint.preflight`'s own item-1 state guard makes this true
    unconditionally, defense in depth)."""
    project_root = init_repo_with_config(tmp_path)
    _start_and_implement(project_root, monkeypatch)
    ai_runs_dir = project_root / ".ai-runs"

    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert load_block_state(ai_runs_dir).last_gate is not None

    # A later gate ATTEMPT fails operationally (both retries exhausted).
    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_TRANSIENT_FAILURE": "1"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 2
    state = load_block_state(ai_runs_dir)
    assert state.state == "BLOCKED_MANUAL"
    assert state.last_gate is None  # revoked, not left stale

    exit_code = run_cli(["checkpoint", "--project-root", str(project_root)])
    assert exit_code == 1
    assert load_block_state(ai_runs_dir).state != "COMPLETED"


def test_blocked_manual_is_never_checkpoint_eligible_even_with_stale_cached_pass(tmp_path: Path) -> None:
    """Item 1's own independent, defense-in-depth guard: `preflight`
    rejects `BLOCKED_MANUAL` FIRST, before even looking at `last_gate`/
    `last_safe_stage` - so even an artificially-reconstructed state where
    stale PASS data was never cleared (a hypothetical future bug in some
    other code path) is still refused."""
    import subprocess
    from dataclasses import replace

    from solari_workflow.config.loader import load_config
    from solari_workflow.fingerprint.engine import compute_fingerprint
    from solari_workflow.git.candidate_tree import build_candidate_tree
    from solari_workflow.git.checkpoint import CheckpointBlockedError, preflight
    from solari_workflow.git.ops import open_repository

    project_root = tmp_path
    subprocess.run(["git", "init", "-q", "-b", "main", str(project_root)], check=True)
    subprocess.run(["git", "-C", str(project_root), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(project_root), "config", "user.name", "Test"], check=True)
    from _lifecycle_helpers import init_repo_with_config

    init_repo_with_config(project_root)
    repo = open_repository(project_root)
    repo.create_branch("T091-Test", start_point="main")
    repo.switch("T091-Test")
    (project_root / "feature.py").write_text("x\n", encoding="utf-8")

    candidate = build_candidate_tree(repo)
    fingerprint = compute_fingerprint(repo, candidate, first_task="T091", last_task="T091")
    config, _ = load_config(project_root / ".ai-workflow.toml")

    from solari_workflow.state.block_state import new_block_state

    state = new_block_state(branch_name="T091-Test", first_task="T091", last_task="T091", block_name="Test")
    # Artificially reconstruct a state that a hypothetical bug might
    # produce: BLOCKED_MANUAL, yet last_gate/last_safe_stage/gated_main_oid
    # left exactly as an eligible PASS would have set them.
    stale_state = replace(
        state,
        state="BLOCKED_MANUAL",
        last_safe_stage="GATE_PASSED",
        last_gate=fingerprint,
        gated_main_oid=repo.rev_parse("main"),
        gate_feature_identity=None,
    )
    with pytest.raises(CheckpointBlockedError) as excinfo:
        preflight(repo, config, stale_state, ai_runs_dir=project_root / ".ai-runs")
    assert "BLOCKED_MANUAL" in str(excinfo.value)
