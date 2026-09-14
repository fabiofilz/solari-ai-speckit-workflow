"""Integration test (T043): pass a gate, mutate the working tree before
`checkpoint`, assert `checkpoint` fails with EXACTLY `CHECKPOINT BLOCKED —
WORKING TREE CHANGED AFTER GATE` and leaves the index untouched; revert
the mutation, re-gate, and confirm `checkpoint` then succeeds.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from solari_workflow.git.checkpoint import CHECKPOINT_FINGERPRINT_MISMATCH_MESSAGE
from solari_workflow.git.ops import open_repository
from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _start_implement_and_gate(project_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0


def test_working_tree_change_after_gate_blocks_checkpoint_with_exact_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project_root = init_repo_with_config(tmp_path)
    _start_implement_and_gate(project_root, monkeypatch)

    repo = open_repository(project_root)
    real_index_oid_before = repo.write_tree()

    # Mutate the working tree AFTER the gate.
    (project_root / "feature.py").write_text("def feature():\n    return 43  # mutated after gate\n", encoding="utf-8")

    exit_code = run_cli(["checkpoint", "--project-root", str(project_root)])
    assert exit_code == 1

    captured = capsys.readouterr()
    assert CHECKPOINT_FINGERPRINT_MISMATCH_MESSAGE in captured.err

    # The real index was never touched.
    assert repo.write_tree() == real_index_oid_before
    assert repo.is_clean_staging_area()
    assert repo.current_branch() == "T091-ValidationPipeline"  # never switched to main

    ai_runs_dir = project_root / ".ai-runs"
    state = load_block_state(ai_runs_dir)
    assert state.last_gate is None
    assert state.last_safe_stage == "IMPLEMENTATION_COMPLETE"

    # Revert the mutation, re-gate, and confirm checkpoint then succeeds.
    (project_root / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0

    exit_code = run_cli(["checkpoint", "--project-root", str(project_root)])
    assert exit_code == 0
    state = load_block_state(ai_runs_dir)
    assert state.state == "COMPLETED"
