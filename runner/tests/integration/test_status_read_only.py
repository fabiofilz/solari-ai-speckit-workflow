"""Regression tests for M4 (`status` must be genuinely read-only - no
temporary Git index, no new Git object, no lock/state mutation)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _object_count(project_root: Path) -> int:
    result = subprocess.run(
        ["git", "-C", str(project_root), "count-objects"], capture_output=True, text=True, check=True
    )
    # Output shape: "<count> objects, <size> kilobytes"
    return int(result.stdout.split()[0])


def test_status_creates_no_new_git_objects_with_an_eligible_gate_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strongest case: a block sitting at `GATE_PASSED` is exactly
    the scenario where the OLD implementation called
    `checkpoint.preflight`, which rebuilds the Candidate Tree - a real
    `git write-tree` that creates a real (if unreferenced) tree object.
    """
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

    subprocess.run(["git", "-C", str(project_root), "gc", "-q"], check=True)  # a clean baseline to count from
    before = _object_count(project_root)

    exit_code = run_cli(["status", "--project-root", str(project_root)])
    assert exit_code == 0

    after = _object_count(project_root)
    assert after == before, "status must never write a new Git object (e.g. via a temporary index write-tree)"


def test_status_never_mutates_block_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0

    ai_runs_dir = project_root / ".ai-runs"
    before = load_block_state(ai_runs_dir)

    assert run_cli(["status", "--project-root", str(project_root)]) == 0

    after = load_block_state(ai_runs_dir)
    assert before == after


def test_status_reports_blocked_without_mutating_anything_when_no_gate_recorded(tmp_path: Path) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0

    subprocess.run(["git", "-C", str(project_root), "gc", "-q"], check=True)
    before = _object_count(project_root)

    exit_code = run_cli(["status", "--project-root", str(project_root)])
    assert exit_code == 0

    after = _object_count(project_root)
    assert after == before
