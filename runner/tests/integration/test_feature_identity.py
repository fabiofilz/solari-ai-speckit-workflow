"""Regression tests for A5 (Spec Kit feature identity must be resolved
unambiguously or not at all - no "highest-numbered directory" heuristic,
fail closed on real ambiguity) and its persistence into block-state."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def test_start_block_fails_closed_on_multiple_feature_directories(tmp_path: Path) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, feature="001-alpha", tasks={"T091": "Alpha task"})
    write_spec_kit_tasks(project_root, feature="002-beta", tasks={"T091": "A completely different Beta task"})

    exit_code = run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    )
    assert exit_code == 2  # manual intervention - ambiguity, never guessed

    # No block-state was written - start-block did not proceed at all.
    assert load_block_state(project_root / ".ai-runs") is None


def test_start_block_resolves_a_single_unambiguous_feature_directory(tmp_path: Path) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, feature="001-only-one", tasks={"T091": "The one true task"})

    exit_code = run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    )
    assert exit_code == 0

    state = load_block_state(project_root / ".ai-runs")
    assert state.tasks_md_path is not None
    assert state.tasks_md_path.endswith("001-only-one/tasks.md") or "001-only-one" in state.tasks_md_path


def test_start_block_fails_closed_on_root_plus_child_ambiguity(tmp_path: Path) -> None:
    """Item 7: a root-level `tasks.md` COEXISTING with a child feature
    directory that also has one is ambiguous - no config field
    designates which one wins, so this must fail closed exactly like
    two child directories would, never silently prefer the root (or the
    child)."""
    project_root = init_repo_with_config(tmp_path)
    spec_dir = project_root / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "tasks.md").write_text("- [ ] T091 Root task\n", encoding="utf-8")
    write_spec_kit_tasks(project_root, feature="001-child", tasks={"T091": "Child task"})

    exit_code = run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    )
    assert exit_code == 2
    assert load_block_state(project_root / ".ai-runs") is None


def test_start_block_proceeds_with_no_tasks_md_at_all(tmp_path: Path) -> None:
    """Absence is not ambiguity - a legitimate degrade to `tasks_md_path
    = None` (task titles unavailable, never a safety concern)."""
    project_root = init_repo_with_config(tmp_path)

    exit_code = run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    )
    assert exit_code == 0
    state = load_block_state(project_root / ".ai-runs")
    assert state.tasks_md_path is None
