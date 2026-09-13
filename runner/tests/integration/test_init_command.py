"""Integration tests for `solari-workflow init` (T022, User Story 1).

Runs the real CLI entry point (`solari_workflow.cli.main`) against an
**actual, disposable, empty Git repository** — never the real project
repository — matching T022's own literal wording ("against an empty temp
repo") and quickstart.md Scenario 1's sequence (`git init` first, `init`
second, kept as two independent steps). Every `git` invocation here
operates only inside a `tmp_path`-rooted disposable repository
(T022-T032 re-gate, Finding 6 — MAJOR: a prior version of this file used
a plain temporary directory with no Git repository at all).
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from solari_workflow.cli import main
from solari_workflow.git.ops import open_repository

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")

# Strings that must never appear in a freshly generated config — evidence
# this workflow repository's own reference project never leaks into a
# scaffolded project's config (spec.md SC-001).
_REFERENCE_PROJECT_MARKERS = ("pdf", "converter", "PDF", "Converter", "PDF-Converter")


def _init_empty_repo(path: Path) -> None:
    """An empty, disposable Git repository (T022: "an empty temp repo") —
    initialized but with no commits, no files, nothing staged."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)


def _read_config(project_root: Path) -> dict:
    with (project_root / ".ai-workflow.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_init_produces_a_schema_valid_config_for_a_new_stack(tmp_path: Path) -> None:
    _init_empty_repo(tmp_path)
    exit_code = main(
        [
            "init",
            "--project-name",
            "throwaway-go-project",
            "--stack",
            "go",
            "--project-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 0

    config_path = tmp_path / ".ai-workflow.toml"
    assert config_path.is_file()

    doc = _read_config(tmp_path)
    assert doc["environment"]["stack"] == "go"
    assert doc["git"]["push"]["mode"] == "manual"
    assert doc["project"]["name"] == "throwaway-go-project"


def test_init_output_contains_no_reference_project_values(tmp_path: Path) -> None:
    _init_empty_repo(tmp_path)
    main(["init", "--project-name", "throwaway-go-project", "--stack", "go", "--project-root", str(tmp_path)])
    content = (tmp_path / ".ai-workflow.toml").read_text(encoding="utf-8")
    for marker in _REFERENCE_PROJECT_MARKERS:
        assert marker not in content


def test_init_config_round_trips_through_the_real_config_loader(tmp_path: Path) -> None:
    """The generated config isn't just well-formed TOML — it must satisfy
    every hard-required-field/enum rule the loader itself enforces."""
    from solari_workflow.config.loader import load_config

    _init_empty_repo(tmp_path)
    main(["init", "--project-name", "throwaway-go-project", "--stack", "go", "--project-root", str(tmp_path)])
    config, warnings = load_config(tmp_path / ".ai-workflow.toml")
    assert config.environment.stack == "go"
    assert warnings == []


def test_init_ensures_ai_runs_is_actually_git_ignored(tmp_path: Path) -> None:
    """Stronger than a textual `.gitignore` check: proves `git` itself
    treats `.ai-runs/` as ignored, via the same `check-ignore` primitive
    the readiness gate's own `ai_runs_git_ignored` check uses
    (`readiness/engine.py`)."""
    _init_empty_repo(tmp_path)
    main(["init", "--project-name", "throwaway-go-project", "--stack", "go", "--project-root", str(tmp_path)])

    gitignore_content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".ai-runs/" in gitignore_content

    repo = open_repository(tmp_path)
    assert repo.is_path_ignored(".ai-runs") is True


def test_init_without_force_refuses_to_overwrite_an_existing_config(tmp_path: Path) -> None:
    _init_empty_repo(tmp_path)
    first = main(["init", "--project-name", "p", "--stack", "go", "--project-root", str(tmp_path)])
    assert first == 0
    original_content = (tmp_path / ".ai-workflow.toml").read_text(encoding="utf-8")

    second = main(["init", "--project-name", "p", "--stack", "node", "--project-root", str(tmp_path)])
    assert second == 1  # exit 1: config already exists (contracts/cli-interface.md)

    # Refused — the existing file must be untouched.
    assert (tmp_path / ".ai-workflow.toml").read_text(encoding="utf-8") == original_content


def test_init_with_force_overwrites_and_shows_a_diff(tmp_path: Path, capsys) -> None:
    _init_empty_repo(tmp_path)
    main(["init", "--project-name", "p", "--stack", "go", "--project-root", str(tmp_path)])
    capsys.readouterr()  # discard first run's output

    exit_code = main(["init", "--project-name", "p", "--stack", "node", "--force", "--project-root", str(tmp_path)])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "-" in captured.out and "+" in captured.out  # a unified diff was shown
    assert 'stack = "go"' in captured.out
    assert 'stack = "node"' in captured.out

    doc = _read_config(tmp_path)
    assert doc["environment"]["stack"] == "node"


def test_init_every_supported_stack_produces_a_valid_config(tmp_path: Path) -> None:
    from solari_workflow.config.loader import load_config

    for stack in ("python", "node", "go", "rust", "other"):
        stack_dir = tmp_path / stack
        stack_dir.mkdir()
        _init_empty_repo(stack_dir)
        exit_code = main(
            ["init", "--project-name", f"project-{stack}", "--stack", stack, "--project-root", str(stack_dir)]
        )
        assert exit_code == 0, f"init failed for stack={stack}"
        config, warnings = load_config(stack_dir / ".ai-workflow.toml")
        assert config.environment.stack == stack
        assert warnings == []


def test_init_preserves_pre_existing_user_gitignore_entries_across_a_forced_rerun(tmp_path: Path) -> None:
    _init_empty_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("node_modules/\n*.log\n", encoding="utf-8")
    main(["init", "--project-name", "p", "--stack", "go", "--project-root", str(tmp_path)])
    main(["init", "--project-name", "p", "--stack", "node", "--force", "--project-root", str(tmp_path)])

    gitignore_content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in gitignore_content
    assert "*.log" in gitignore_content
    assert ".ai-runs/" in gitignore_content


# --- Failure atomicity (T022-T032 re-gate, Finding 5 — MINOR) ---------------
#
# These exercise `init`'s own file-write atomicity, a concern orthogonal to
# Finding 6's "run against a real repo" — a plain disposable directory is
# sufficient (Git's presence or absence does not affect this behavior), so
# they deliberately do NOT call `_init_empty_repo`.
#
# `.gitignore` write failure is deterministically simulated by pre-creating
# `.gitignore` as a DIRECTORY (a real, cross-platform-realistic write
# failure — `os.replace` onto an existing directory raises `IsADirectoryError`
# on POSIX) rather than monkeypatching internals — this exercises the real
# atomic-write/rollback code path end-to-end.


def test_init_gitignore_failure_leaves_a_freshly_created_config_rolled_back(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").mkdir()  # forces the .gitignore write step to fail

    exit_code = main(["init", "--project-name", "p", "--stack", "go", "--project-root", str(tmp_path)])

    assert exit_code == 2  # manual intervention — an unexpected operational failure
    # No config was left behind: it did not exist before this call, so
    # rollback means "not present", never a half-applied file.
    assert not (tmp_path / ".ai-workflow.toml").exists()


def test_init_gitignore_failure_during_forced_replacement_restores_the_original_config(tmp_path: Path) -> None:
    first = main(["init", "--project-name", "p", "--stack", "go", "--project-root", str(tmp_path)])
    assert first == 0
    original_content = (tmp_path / ".ai-workflow.toml").read_text(encoding="utf-8")

    (tmp_path / ".gitignore").unlink()
    (tmp_path / ".gitignore").mkdir()  # forces the .gitignore write step to fail on the forced rerun

    exit_code = main(
        ["init", "--project-name", "p", "--stack", "node", "--force", "--project-root", str(tmp_path)]
    )

    assert exit_code == 2
    # The forced replacement must have been rolled back to the ORIGINAL
    # config content — never left as the new (go->node) content, and
    # never silently discarded to something else.
    assert (tmp_path / ".ai-workflow.toml").read_text(encoding="utf-8") == original_content


def test_init_normal_success_path_still_works_unaffected_by_atomicity_changes(tmp_path: Path) -> None:
    exit_code = main(["init", "--project-name", "p", "--stack", "go", "--project-root", str(tmp_path)])
    assert exit_code == 0
    assert (tmp_path / ".ai-workflow.toml").is_file()
    assert ".ai-runs/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_init_rerun_refusal_still_works_unaffected_by_atomicity_changes(tmp_path: Path) -> None:
    main(["init", "--project-name", "p", "--stack", "go", "--project-root", str(tmp_path)])
    exit_code = main(["init", "--project-name", "p", "--stack", "node", "--project-root", str(tmp_path)])
    assert exit_code == 1
