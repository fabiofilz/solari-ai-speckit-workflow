"""Integration test: `readiness-check` must not claim an overall PASS for a
stack whose stack-specific checks (Phase 6, T054-T063) are not implemented
yet in this build (Codex finding 3B).

Invokes the real `solari-workflow` console entry point as a subprocess
against a disposable temporary project + Git repository — never the real
project repository.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")

_CONFIG_TEMPLATE = """
[workflow]
schema_version = "1"
source = "https://example.invalid"
version = "1.0.0"

[project]
name = "example"
type = "service"
main_branch = "main"

[speckit]
spec_dir = "specs"

[environment]
stack = "{stack}"
runtime_version = "1"
dependency_manager = "none"
manifest = "none"
lockfile_must_be_committed = true

[git]
checkpoint_merge_strategy = "no-ff"

[git.push]
mode = "manual"

[checkpoint]
tag_prefix = "checkpoint"
blocking_severities = ["blocker", "major"]

[branch_retention]
keep_recent_merged = 2
keep_active = true

[ai_runs]
directory = ".ai-runs"
git_ignored = true
"""


def _init_project(project_root: Path, *, stack: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(project_root)], check=True)
    subprocess.run(["git", "-C", str(project_root), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(project_root), "config", "user.name", "Test"], check=True)
    (project_root / ".ai-workflow.toml").write_text(
        _CONFIG_TEMPLATE.format(stack=stack), encoding="utf-8"
    )
    (project_root / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    (project_root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(project_root), "add", ".ai-workflow.toml", ".gitignore", "README.md"], check=True
    )
    subprocess.run(["git", "-C", str(project_root), "commit", "-q", "-m", "initial"], check=True)


def _run_readiness_check(project_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "solari_workflow", "readiness-check", "--project-root", str(project_root)],
        capture_output=True,
        text=True,
    )


def test_readiness_check_cannot_claim_pass_for_a_stack_without_registered_checks(tmp_path: Path) -> None:
    _init_project(tmp_path, stack="python")

    result = _run_readiness_check(tmp_path)

    assert result.returncode == 2
    assert result.returncode not in (0, 1)
    assert "not implemented" in result.stderr.lower() or "cannot yet" in result.stderr.lower()
    # The (accurate) common-check results are still surfaced in full.
    assert "config_valid" in result.stdout


@pytest.mark.parametrize("stack", ["node", "go", "rust"])
def test_readiness_check_cannot_claim_pass_for_any_pending_stack(tmp_path: Path, stack: str) -> None:
    _init_project(tmp_path, stack=stack)
    result = _run_readiness_check(tmp_path)
    assert result.returncode == 2


def test_readiness_check_still_works_normally_for_the_other_stack(tmp_path: Path) -> None:
    """`"other"` never needs stack-specific checks, by design — it must
    keep returning a real overall PASS/FAIL, not the capability error."""
    _init_project(tmp_path, stack="other")

    result = _run_readiness_check(tmp_path)

    assert result.returncode in (0, 1)
