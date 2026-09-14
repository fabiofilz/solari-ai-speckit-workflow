"""Regression tests for A2 (Codex's review evidence must be derived from
Candidate Tree X itself - built FIRST - not a live working-tree diff that
silently omits new file content)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _latest_prompt_content(ai_runs_dir: Path, marker: str) -> str:
    candidates = [p for p in ai_runs_dir.glob("*-prompt.md") if marker in p.name]
    assert candidates, f"no prompt file found matching {marker!r} under {ai_runs_dir}"
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return latest.read_text(encoding="utf-8")


def test_codex_review_evidence_includes_new_file_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0

    # A brand-new, untracked, non-ignored file - the exact case a plain
    # `git diff HEAD` against the live working directory would silently
    # omit entirely (the OLD, remediated behavior).
    marker_content = "UNIQUE_MARKER_CONTENT_0xABCDEF_should_appear_in_the_review_prompt"
    (project_root / "brand_new_file.py").write_text(f"# {marker_content}\n", encoding="utf-8")

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

    ai_runs_dir = project_root / ".ai-runs"
    prompt_content = _latest_prompt_content(ai_runs_dir, "run-codex-gate")

    assert "brand_new_file.py" in prompt_content
    assert marker_content in prompt_content, "the new file's CONTENT, not merely its name, must appear"
    assert "candidate_tree_oid" in prompt_content
    assert "head_oid" in prompt_content


def test_codex_review_evidence_reflects_a_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    (project_root / "doomed.txt").write_text("will be deleted\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project_root), "add", "doomed.txt"], check=True)
    subprocess.run(["git", "-C", str(project_root), "commit", "-q", "-m", "seed"], check=True)

    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0
    (project_root / "doomed.txt").unlink()
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

    prompt_content = _latest_prompt_content(project_root / ".ai-runs", "run-codex-gate")
    assert "doomed.txt" in prompt_content
    assert "will be deleted" in prompt_content  # the deleted content itself is shown in the diff


def test_codex_review_evidence_binds_binary_additions_to_candidate_tree_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 5: a NEW binary file must never be silently invisible - a
    plain text diff only says "Binary files ... differ"; the prompt must
    ALSO carry deterministic, integrity-bound identity (path, mode,
    candidate-tree blob oid, size) - never the raw binary bytes
    themselves."""
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0

    binary_content = bytes(range(256)) * 4  # unambiguously binary (embedded NUL bytes)
    (project_root / "image.bin").write_bytes(binary_content)

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

    ai_runs_dir = project_root / ".ai-runs"
    prompt_content = _latest_prompt_content(ai_runs_dir, "run-codex-gate")

    assert "image.bin" in prompt_content
    assert "binary" in prompt_content.lower()
    assert "candidate-tree blob oid" in prompt_content
    assert f"{len(binary_content)} bytes" in prompt_content
    # The raw binary bytes themselves must never be dumped into the
    # (UTF-8, Markdown) prompt file.
    assert "\x00" not in prompt_content


def test_codex_review_evidence_binds_a_binary_deletion_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    (project_root / "old_image.bin").write_bytes(bytes(range(256)))
    subprocess.run(["git", "-C", str(project_root), "add", "old_image.bin"], check=True)
    subprocess.run(["git", "-C", str(project_root), "commit", "-q", "-m", "seed a binary file"], check=True)

    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0
    (project_root / "old_image.bin").unlink()
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

    prompt_content = _latest_prompt_content(project_root / ".ai-runs", "run-codex-gate")
    assert "old_image.bin" in prompt_content
    assert "DELETED" in prompt_content
