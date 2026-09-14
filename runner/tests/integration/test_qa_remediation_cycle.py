"""Integration test (T042): a Codex `FAIL` with a blocking-severity finding
-> a simulated orchestrator-initiated `run-claude` remediation on the
SAME branch (no new branch created) -> a fresh `run-codex-gate` (new
Fingerprint) -> `PASS` -> `checkpoint` succeeds.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from solari_workflow.git.ops import open_repository
from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def test_qa_remediation_cycle_stays_on_the_same_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})

    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0

    repo = open_repository(project_root)
    branch_before = repo.current_branch()

    (project_root / "feature.py").write_text("def feature():\n    return None  # bug\n", encoding="utf-8")
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0

    # First gate: FAIL with a blocking finding.
    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "FAIL", "FAKE_CODEX_FINDINGS": "blocker:returns None instead of 42"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 1

    ai_runs_dir = project_root / ".ai-runs"
    state = load_block_state(ai_runs_dir)
    assert state.state == "QA_REMEDIATION_REQUIRED"
    assert state.last_gate is None  # a FAIL never populates last_gate

    # Checkpoint must refuse - no eligible gate recorded.
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 1

    # Remediation - simulated orchestrator-initiated run-claude on the SAME branch.
    assert repo.current_branch() == branch_before
    (project_root / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "fix the blocker finding", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert repo.current_branch() == branch_before  # never a new branch

    # Fresh re-gate: PASS, a NEW Fingerprint.
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0

    state = load_block_state(ai_runs_dir)
    assert state.last_safe_stage == "GATE_PASSED"
    assert state.last_gate is not None

    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 0
    state = load_block_state(ai_runs_dir)
    assert state.state == "COMPLETED"
