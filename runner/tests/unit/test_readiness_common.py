"""Unit tests for readiness common checks (T019).

Git-dependent checks are exercised against isolated, disposable
temporary repositories (`tmp_path` + `git init`) — never the real project
repository.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow.config.schema import (
    AiRunsSection,
    BranchRetentionSection,
    CheckpointSection,
    EnvironmentSection,
    GitPushSection,
    GitSection,
    ProjectSection,
    SpeckitSection,
    Stack,
    WorkflowConfig,
    WorkflowSection,
)
from solari_workflow.readiness.engine import (
    check_ai_runs_git_ignored,
    check_checkpoint_strategy_defined,
    check_config_valid,
    check_main_branch_identified,
    check_no_obvious_secrets,
    run_readiness_gate,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _base_config(**overrides: object) -> WorkflowConfig:
    defaults = dict(
        workflow=WorkflowSection(schema_version="1", source="https://example.invalid", version="1.0.0"),
        project=ProjectSection(name="example", type="service", main_branch="main"),
        speckit=SpeckitSection(spec_dir="specs"),
        environment=EnvironmentSection(
            stack="other",
            runtime_version="1",
            dependency_manager="none",
            manifest="none",
            lockfile_must_be_committed=True,
        ),
        git=GitSection(checkpoint_merge_strategy="no-ff", push=GitPushSection(mode="manual")),
        checkpoint=CheckpointSection(tag_prefix="checkpoint", blocking_severities=["blocker", "major"]),
        branch_retention=BranchRetentionSection(keep_recent_merged=2, keep_active=True),
        ai_runs=AiRunsSection(directory=".ai-runs", git_ignored=True),
    )
    defaults.update(overrides)
    return WorkflowConfig(**defaults)  # type: ignore[arg-type]


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)


def test_check_config_valid_always_passes_once_reached() -> None:
    result = check_config_valid(_base_config(), Path("."))
    assert result.status == "PASS"


def test_check_main_branch_identified_passes_when_set() -> None:
    result = check_main_branch_identified(_base_config(), Path("."))
    assert result.status == "PASS"
    assert "main" in result.message


def test_check_main_branch_identified_fails_when_empty() -> None:
    config = _base_config(project=ProjectSection(name="x", type="service", main_branch=""))
    result = check_main_branch_identified(config, Path("."))
    assert result.status == "FAIL"
    assert result.severity == "blocking"


def test_check_checkpoint_strategy_defined_passes_for_no_ff() -> None:
    result = check_checkpoint_strategy_defined(_base_config(), Path("."))
    assert result.status == "PASS"


def test_check_no_obvious_secrets_passes_for_clean_config() -> None:
    result = check_no_obvious_secrets(_base_config(), Path("."))
    assert result.status == "PASS"


def test_check_no_obvious_secrets_flags_sk_shaped_string() -> None:
    config = _base_config(
        project=ProjectSection(name="x", type="service", main_branch="main"),
        workflow=WorkflowSection(
            schema_version="1",
            source="https://example.invalid",
            version="sk-1234567890abcdef",
        ),
    )
    result = check_no_obvious_secrets(config, Path("."))
    assert result.status == "FAIL"
    assert result.severity == "warning"


def test_check_no_obvious_secrets_flags_aws_key_shaped_string() -> None:
    config = _base_config(
        project=ProjectSection(name="AKIAABCDEFGHIJKLMNOP", type="service", main_branch="main")
    )
    result = check_no_obvious_secrets(config, Path("."))
    assert result.status == "FAIL"


def test_check_ai_runs_git_ignored_passes_when_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".ai-runs").mkdir()
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")

    result = check_ai_runs_git_ignored(_base_config(), tmp_path)
    assert result.status == "PASS"


def test_check_ai_runs_git_ignored_fails_when_tracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ai_runs_dir = tmp_path / ".ai-runs"
    ai_runs_dir.mkdir()
    (ai_runs_dir / "20260101-001-x-prompt.md").write_text("oops", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".ai-runs"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "accidentally tracked"], check=True)

    result = check_ai_runs_git_ignored(_base_config(), tmp_path)
    assert result.status == "FAIL"
    assert result.severity == "blocking"


def test_check_ai_runs_git_ignored_fails_closed_outside_a_repository(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    result = check_ai_runs_git_ignored(_base_config(), plain_dir)
    assert result.status == "FAIL"


def test_check_ai_runs_git_ignored_fails_when_untracked_but_not_ignored(tmp_path: Path) -> None:
    """Regression for the Codex-reproduced false PASS: a `.ai-runs/`
    directory that is merely untracked (never `git add`-ed) but not
    covered by any `.gitignore` rule must FAIL — the old tracked-files-
    only check incorrectly PASSed exactly this case."""
    _init_repo(tmp_path)
    (tmp_path / ".ai-runs").mkdir()
    # Deliberately no .gitignore entry for .ai-runs/ at all.
    result = check_ai_runs_git_ignored(_base_config(), tmp_path)
    assert result.status == "FAIL"
    assert result.severity == "blocking"
    assert "not excluded" in result.message


def test_check_ai_runs_git_ignored_passes_even_before_the_directory_exists_on_disk(tmp_path: Path) -> None:
    """A fresh project's `.ai-runs/` may not exist yet at readiness-check
    time (the runner creates it lazily); the ignored check must still
    correctly PASS from the `.gitignore` rule alone."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    assert not (tmp_path / ".ai-runs").exists()

    result = check_ai_runs_git_ignored(_base_config(), tmp_path)
    assert result.status == "PASS"


def _config_for_stack(stack: Stack) -> WorkflowConfig:
    return _base_config(
        environment=EnvironmentSection(
            stack=stack,
            runtime_version="1",
            dependency_manager="none",
            manifest="none",
            lockfile_must_be_committed=True,
        )
    )


def test_stack_checks_registered_is_true_for_other_stack(tmp_path: Path) -> None:
    """`"other"` never needs stack-specific checks, by design — this is a
    permanent PASS-eligible state, not an incompleteness gap."""
    _init_repo(tmp_path)
    result = run_readiness_gate(_config_for_stack("other"), tmp_path)
    assert result.stack_checks_registered is True


@pytest.mark.parametrize("stack", ["python", "node", "go", "rust"])
def test_stack_checks_registered_is_false_before_phase_6_populates_the_registry(
    tmp_path: Path, stack: str
) -> None:
    """python/node/go/rust readiness checks (T054-T063) are not implemented
    yet — the gate must say so rather than implying a complete PASS is
    possible for these stacks in this build."""
    _init_repo(tmp_path)
    result = run_readiness_gate(_config_for_stack(stack), tmp_path)  # type: ignore[arg-type]
    assert result.stack_checks_registered is False


@pytest.mark.parametrize("stack", ["python", "node", "go", "rust"])
def test_engine_overall_status_is_not_pass_for_an_unregistered_stack_even_with_clean_config(
    tmp_path: Path, stack: str
) -> None:
    """Regression for the MAJOR re-gate finding: the ENGINE itself (not
    only a calling CLI subcommand) must fail closed. Every common check
    passes here (a fully clean, correctly configured project), yet
    `overall_status` must still never be `"PASS"` for a stack whose
    stack-specific checks are not registered — a caller inspecting only
    `overall_status` must not be able to interpret the project as ready.
    """
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    result = run_readiness_gate(_config_for_stack(stack), tmp_path)  # type: ignore[arg-type]
    assert all(check.status == "PASS" for check in result.checks)  # nothing is actually broken...
    assert result.overall_status != "PASS"  # ...yet overall must still not claim PASS
    assert result.overall_status == "INCOMPLETE"


def test_engine_overall_status_is_incomplete_not_fail_when_only_the_stack_is_unregistered(
    tmp_path: Path,
) -> None:
    """`"INCOMPLETE"` is a distinct outcome from `"FAIL"` — an unregistered
    stack with an otherwise-clean config is not reported as broken."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    result = run_readiness_gate(_config_for_stack("python"), tmp_path)
    assert result.overall_status == "INCOMPLETE"
    assert result.overall_status != "FAIL"


def test_engine_overall_status_prefers_fail_over_incomplete_when_both_apply(tmp_path: Path) -> None:
    """A real, actionable FAIL always takes priority over "incomplete"."""
    _init_repo(tmp_path)
    # No .gitignore for .ai-runs/ at all -> a genuine, actionable FAIL,
    # on top of "python" also being an unregistered stack.
    config = _config_for_stack("python")
    result = run_readiness_gate(config, tmp_path)
    assert result.overall_status == "FAIL"


def test_engine_overall_status_becomes_pass_capable_once_a_stack_is_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once Phase 6 (T054-T063) registers a stack's checks,
    `run_readiness_gate` must resume normal PASS/FAIL evaluation for it
    with no further change to this module."""
    import solari_workflow.readiness.engine as engine_module

    def _fake_python_check(_config: WorkflowConfig, _project_root: Path):
        from solari_workflow.readiness.engine import CheckResult

        return CheckResult("fake_python_check", "PASS", "pretend stack-specific check")

    monkeypatch.setitem(engine_module.STACK_REGISTRY, "python", [_fake_python_check])

    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    result = run_readiness_gate(_config_for_stack("python"), tmp_path)

    assert result.stack_checks_registered is True
    assert result.overall_status == "PASS"
    assert any(c.name == "fake_python_check" for c in result.checks)


def test_engine_overall_status_still_fails_normally_once_a_stack_is_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal FAIL evaluation, not just PASS, resumes once registered."""
    import solari_workflow.readiness.engine as engine_module

    def _fake_failing_python_check(_config: WorkflowConfig, _project_root: Path):
        from solari_workflow.readiness.engine import CheckResult

        return CheckResult("fake_python_check", "FAIL", "pretend stack-specific failure", "blocking")

    monkeypatch.setitem(engine_module.STACK_REGISTRY, "python", [_fake_failing_python_check])

    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    result = run_readiness_gate(_config_for_stack("python"), tmp_path)

    assert result.stack_checks_registered is True
    assert result.overall_status == "FAIL"


# --- Empty-registry-entry regression (3rd re-gate MAJOR finding) ---
#
# A registry KEY alone (`STACK_REGISTRY["python"] = []`) is not a complete
# stack readiness implementation: zero checks ran, so nothing has actually
# verified that stack. This must be indistinguishable in effect from no
# entry at all — never enough to unlock a `PASS`.


@pytest.mark.parametrize("stack", ["python", "node", "go", "rust"])
def test_engine_overall_status_is_incomplete_for_a_missing_registry_entry(
    tmp_path: Path, stack: str
) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    result = run_readiness_gate(_config_for_stack(stack), tmp_path)  # type: ignore[arg-type]
    assert result.stack_checks_registered is False
    assert result.overall_status == "INCOMPLETE"


@pytest.mark.parametrize("stack", ["python", "node", "go", "rust"])
def test_engine_overall_status_is_incomplete_for_an_empty_registry_list(
    tmp_path: Path, stack: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the Codex finding exactly: `STACK_REGISTRY[stack] = []`
    with every common check passing must still NOT be `PASS`."""
    import solari_workflow.readiness.engine as engine_module

    monkeypatch.setitem(engine_module.STACK_REGISTRY, stack, [])

    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    result = run_readiness_gate(_config_for_stack(stack), tmp_path)  # type: ignore[arg-type]

    assert all(c.status == "PASS" for c in result.checks)  # every common check passed...
    assert result.stack_checks_registered is False  # ...but an empty list is not "registered"...
    assert result.overall_status == "INCOMPLETE"  # ...so overall must not be PASS
    assert result.overall_status != "PASS"


def test_engine_overall_status_is_fail_when_a_common_check_fails_with_an_empty_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine common-check FAIL still takes precedence over INCOMPLETE,
    even when the registry entry is present-but-empty rather than absent."""
    import solari_workflow.readiness.engine as engine_module

    monkeypatch.setitem(engine_module.STACK_REGISTRY, "python", [])

    _init_repo(tmp_path)
    # Deliberately no .gitignore for .ai-runs/ -> a genuine common-check FAIL.
    config = _config_for_stack("python")
    result = run_readiness_gate(config, tmp_path)

    assert result.overall_status == "FAIL"


def test_run_readiness_gate_overall_pass_when_all_checks_pass(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # A properly configured project actually excludes .ai-runs/ via .gitignore
    # (check_ai_runs_git_ignored now verifies this for real — see the
    # dedicated tests above — rather than merely checking for tracked files).
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    result = run_readiness_gate(_base_config(), tmp_path)
    assert result.overall_status == "PASS"
    assert all(check.status != "FAIL" for check in result.checks)
    assert len(result.checks) == 5  # exactly the common checks for an unregistered stack


def test_run_readiness_gate_overall_fail_when_any_check_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    config = _base_config(project=ProjectSection(name="x", type="service", main_branch=""))
    result = run_readiness_gate(config, tmp_path)
    assert result.overall_status == "FAIL"


def test_run_readiness_gate_reports_config_version_and_timestamp(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = run_readiness_gate(_base_config(), tmp_path)
    assert result.config_version == "1.0.0"
    assert result.checked_at.endswith("Z")
