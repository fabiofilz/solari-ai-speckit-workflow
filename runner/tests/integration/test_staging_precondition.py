"""Integration test (T045): a stray manual `git add` before
`run-codex-gate`/`checkpoint` is refused (exit `2`), names the
unexpectedly staged path, and `git diff --cached --name-only` still shows
it afterward - the runner never ran `git reset` or any other
index-mutating recovery on its own.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _stray_stage(project_root: Path) -> None:
    (project_root / "stray.txt").write_text("staged unrelated content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project_root), "add", "stray.txt"], check=True)


def _staged_paths(project_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(project_root), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_stray_staged_change_refuses_run_codex_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    _stray_stage(project_root)

    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 2
    assert _staged_paths(project_root) == ["stray.txt"]  # never reset/reverted by the runner


def test_stray_staged_change_refuses_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    _stray_stage(project_root)

    exit_code = run_cli(["checkpoint", "--project-root", str(project_root)])
    assert exit_code == 2
    assert _staged_paths(project_root) == ["stray.txt"]


def test_stray_staged_change_refuses_start_block(tmp_path: Path) -> None:
    project_root = init_repo_with_config(tmp_path)
    _stray_stage(project_root)

    exit_code = run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    )
    assert exit_code == 2
    assert _staged_paths(project_root) == ["stray.txt"]
