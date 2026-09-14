"""Regression test for item 4 (checkpoint is bound to the main identity
that was gated): if `main` advances after `run-codex-gate` recorded an
eligible PASS - e.g. a different block's checkpoint merged in the
meantime - `checkpoint` MUST fail closed BEFORE any ref mutation, never
attempting a merge against a base Codex never reviewed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow.git.checkpoint import CHECKPOINT_MAIN_ADVANCED_MESSAGE
from solari_workflow.git.ops import open_repository
from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def test_checkpoint_refuses_when_main_advanced_since_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project_root = init_repo_with_config(tmp_path)
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

    repo = open_repository(project_root)
    ai_runs_dir = project_root / ".ai-runs"
    gated_main_oid = load_block_state(ai_runs_dir).gated_main_oid
    assert gated_main_oid is not None

    # Simulate a DIFFERENT block's checkpoint advancing main in the
    # meantime - a real, out-of-band commit on main, unrelated to this
    # block's own branch.
    subprocess.run(["git", "-C", str(project_root), "switch", "main"], check=True)
    (project_root / "unrelated.txt").write_text("someone else's checkpoint\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project_root), "add", "unrelated.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(project_root), "commit", "-q", "-m", "unrelated concurrent checkpoint"], check=True
    )
    assert repo.rev_parse("main") != gated_main_oid
    subprocess.run(["git", "-C", str(project_root), "switch", "T091-ValidationPipeline"], check=True)

    exit_code = run_cli(["checkpoint", "--project-root", str(project_root)])
    assert exit_code == 1

    captured = capsys.readouterr()
    assert CHECKPOINT_MAIN_ADVANCED_MESSAGE in captured.err

    # No mutation happened at all - main is untouched beyond the
    # unrelated commit, the block branch never advanced, no tag exists.
    assert repo.current_branch() == "T091-ValidationPipeline"
    assert not repo.tag_exists("checkpoint-T091-T091")

    state = load_block_state(ai_runs_dir)
    assert state.state != "COMPLETED"


def test_checkpoint_succeeds_when_main_is_unchanged_since_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check: the item-4 guard does not block the ordinary case
    where main genuinely has not moved."""
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
