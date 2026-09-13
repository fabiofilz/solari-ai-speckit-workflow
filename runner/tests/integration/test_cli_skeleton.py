"""Integration test for the CLI skeleton (T021).

Invokes the installed `solari-workflow` console entry point as a real
subprocess — this exercises argparse's own behavior end-to-end (help
text, usage errors, exit codes), not just the in-process parser object.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

DECLARED_SUBCOMMANDS = [
    "init",
    "readiness-check",
    "start-block",
    "run-claude",
    "run-codex-gate",
    "checkpoint",
    "resume",
    "retention",
    "status",
]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "solari_workflow", *args],
        capture_output=True,
        text=True,
    )


def test_root_help_succeeds_and_lists_every_subcommand() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0
    for subcommand in DECLARED_SUBCOMMANDS:
        assert subcommand in result.stdout


@pytest.mark.parametrize("subcommand", DECLARED_SUBCOMMANDS)
def test_every_declared_subcommand_responds_to_help_successfully(subcommand: str) -> None:
    result = _run_cli(subcommand, "--help")
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


def test_unknown_subcommand_fails_with_usage_error_and_nonzero_exit() -> None:
    result = _run_cli("not-a-real-subcommand")
    assert result.returncode != 0
    assert "invalid choice" in result.stderr.lower() or "usage" in result.stderr.lower()


def test_unknown_flag_fails_with_usage_error_and_nonzero_exit() -> None:
    result = _run_cli("readiness-check", "--not-a-real-flag")
    assert result.returncode != 0
    assert "unrecognized" in result.stderr.lower() or "usage" in result.stderr.lower()


def test_missing_required_flag_fails_with_usage_error() -> None:
    result = _run_cli("start-block", "--first-task", "T001")
    assert result.returncode != 0
    assert "required" in result.stderr.lower() or "usage" in result.stderr.lower()


def test_no_subcommand_at_all_fails_rather_than_hanging() -> None:
    result = _run_cli()
    assert result.returncode != 0


def test_unimplemented_subcommand_reports_actionable_error_and_manual_intervention_exit_code() -> None:
    result = _run_cli("status")
    assert result.returncode == 2
    assert "later phase" in result.stderr.lower()


def test_readiness_check_flag_exists_and_is_optional() -> None:
    result = _run_cli("readiness-check", "--help")
    assert result.returncode == 0
    assert "--project-root" in result.stdout
