"""Regression tests for M3 (illegal lifecycle-state transitions must be
refused, with an actionable error, rather than silently allowed)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def test_run_codex_gate_before_run_claude_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0

    # Claude has not run at all yet - last_safe_stage is still BRANCH_CREATED.
    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 2

    state = load_block_state(project_root / ".ai-runs")
    assert state.last_safe_stage == "BRANCH_CREATED"  # never advanced
    assert state.state == "RUNNING"  # not silently mutated into a gate-relevant state


def test_start_block_does_not_silently_replace_an_unfinished_active_block(tmp_path: Path) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "First block", "T092": "Second block"})

    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "First Block", "--project-root", str(project_root),
        ]
    ) == 0
    first_state = load_block_state(project_root / ".ai-runs")
    assert first_state.branch_name == "T091-FirstBlock"

    # A second start-block, for a DIFFERENT range, while the first is
    # still unfinished (state RUNNING) must be refused - never silently
    # overwrite the first block's own tracking.
    exit_code = run_cli(
        [
            "start-block", "--first-task", "T092", "--last-task", "T092",
            "--block-name", "Second Block", "--project-root", str(project_root),
        ]
    )
    assert exit_code == 2

    state_after = load_block_state(project_root / ".ai-runs")
    assert state_after.branch_name == "T091-FirstBlock"  # unchanged


def test_start_block_is_allowed_after_the_prior_block_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "First block", "T092": "Second block"})

    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "First Block", "--project-root", str(project_root),
        ]
    ) == 0
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 0
    assert load_block_state(project_root / ".ai-runs").state == "COMPLETED"

    exit_code = run_cli(
        [
            "start-block", "--first-task", "T092", "--last-task", "T092",
            "--block-name", "Second Block", "--project-root", str(project_root),
        ]
    )
    assert exit_code == 0
    assert load_block_state(project_root / ".ai-runs").branch_name == "T092-SecondBlock"


def test_run_codex_gate_refused_after_checkpoint_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 0

    # The block is COMPLETED; a stray run-codex-gate must be refused
    # rather than silently gating a finished block again.
    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 2


def test_run_claude_refused_after_checkpoint_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 10: run-claude must reject an already-COMPLETED block, not
    only run-codex-gate."""
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 0

    exit_code = run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "more work", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 2


def test_run_claude_refused_while_gate_passed_stands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 10: run-claude must reject a block sitting at an eligible,
    un-invalidated GATE_PASSED - formal remediation semantics require a
    fresh, superseding gate result to invalidate it first (B1), never
    silent further implementation on top of an already-passed gate."""
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0

    exit_code = run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "one more tweak", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 2
    state = load_block_state(project_root / ".ai-runs")
    assert state.last_safe_stage == "GATE_PASSED"  # unchanged - never silently invalidated
