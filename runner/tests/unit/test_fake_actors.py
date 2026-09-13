"""Unit tests for the fake `claude`/`codex` CLI stubs themselves (T026).

These prove the fixtures used by every later actor test (T034/T036, later
phases) behave exactly as documented, invoked exactly the way a real actor
module will invoke them: `sys.executable <script>`, no shell, controlled
via environment variables layered on top of a clean environment copy.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FAKE_CLAUDE = FIXTURES_DIR / "fake_claude.py"
FAKE_CODEX = FIXTURES_DIR / "fake_codex.py"


def _run(script: Path, env_overrides: dict[str, str], argv_extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **env_overrides}
    argv = [sys.executable, str(script), *(argv_extra or [])]
    return subprocess.run(argv, env=env, capture_output=True, text=True, shell=False)


# --- fake_claude.py ---------------------------------------------------


def test_fake_claude_default_emits_status_complete_and_exits_zero() -> None:
    result = _run(FAKE_CLAUDE, {})
    assert result.returncode == 0
    assert "STATUS: COMPLETE" in result.stdout


def test_fake_claude_reports_architectural_decision_required() -> None:
    result = _run(FAKE_CLAUDE, {"FAKE_CLAUDE_STATUS": "ARCHITECTURAL_DECISION_REQUIRED"})
    assert result.returncode == 0
    assert "STATUS: ARCHITECTURAL_DECISION_REQUIRED" in result.stdout


def test_fake_claude_reports_blocked() -> None:
    result = _run(FAKE_CLAUDE, {"FAKE_CLAUDE_STATUS": "BLOCKED"})
    assert "STATUS: BLOCKED" in result.stdout


def test_fake_claude_missing_marker_emits_no_status_line() -> None:
    result = _run(FAKE_CLAUDE, {"FAKE_CLAUDE_STATUS": "MISSING"})
    assert "STATUS:" not in result.stdout


def test_fake_claude_malformed_marker_is_unparseable_shaped() -> None:
    result = _run(FAKE_CLAUDE, {"FAKE_CLAUDE_STATUS": "MALFORMED"})
    assert "STATUS: not-a-real-value" in result.stdout


def test_fake_claude_exit_code_is_independent_of_status() -> None:
    result = _run(FAKE_CLAUDE, {"FAKE_CLAUDE_STATUS": "COMPLETE", "FAKE_CLAUDE_EXIT_CODE": "2"})
    assert result.returncode == 2
    assert "STATUS: COMPLETE" in result.stdout


def test_fake_claude_transient_failure_takes_priority_and_emits_no_marker() -> None:
    result = _run(
        FAKE_CLAUDE,
        {
            "FAKE_CLAUDE_TRANSIENT_FAILURE": "1",
            "FAKE_CLAUDE_STATUS": "COMPLETE",
            "FAKE_CLAUDE_EXIT_CODE": "0",
        },
    )
    assert result.returncode == 17
    assert "STATUS:" not in result.stdout
    assert "simulated transient launch failure" in result.stderr


def test_fake_claude_transcript_is_emitted_before_the_marker() -> None:
    result = _run(FAKE_CLAUDE, {"FAKE_CLAUDE_TRANSCRIPT": "doing the work"})
    lines = [line for line in result.stdout.splitlines() if line]
    assert lines[0] == "doing the work"
    assert lines[-1] == "STATUS: COMPLETE"


def test_fake_claude_ignores_unknown_argv() -> None:
    result = _run(FAKE_CLAUDE, {}, argv_extra=["--model", "fake", "--effort", "high", "--unrecognized-flag"])
    assert result.returncode == 0
    assert "STATUS: COMPLETE" in result.stdout


# --- fake_codex.py ------------------------------------------------------


def test_fake_codex_default_emits_pass_with_no_findings() -> None:
    result = _run(FAKE_CODEX, {})
    assert result.returncode == 0
    assert "RESULT: PASS" in result.stdout
    assert "FINDINGS: []" in result.stdout


def test_fake_codex_reports_fail() -> None:
    result = _run(FAKE_CODEX, {"FAKE_CODEX_RESULT": "FAIL"})
    assert "RESULT: FAIL" in result.stdout


def test_fake_codex_missing_block_emits_nothing() -> None:
    result = _run(FAKE_CODEX, {"FAKE_CODEX_RESULT": "MISSING"})
    assert "RESULT:" not in result.stdout
    assert "FINDINGS:" not in result.stdout


def test_fake_codex_malformed_result_is_case_mismatched() -> None:
    result = _run(FAKE_CODEX, {"FAKE_CODEX_RESULT": "MALFORMED"})
    assert "RESULT: Pass" in result.stdout  # not the exact-case "PASS" the contract requires


def test_fake_codex_renders_findings_with_severity_and_summary() -> None:
    result = _run(
        FAKE_CODEX,
        {"FAKE_CODEX_FINDINGS": "blocker:missing null check;minor:typo in comment"},
    )
    assert "- severity: blocker" in result.stdout
    assert "summary: missing null check" in result.stdout
    assert "- severity: minor" in result.stdout
    assert "summary: typo in comment" in result.stdout


def test_fake_codex_exit_code_is_independent_of_result() -> None:
    result = _run(FAKE_CODEX, {"FAKE_CODEX_RESULT": "PASS", "FAKE_CODEX_EXIT_CODE": "1"})
    assert result.returncode == 1
    assert "RESULT: PASS" in result.stdout


def test_fake_codex_transient_failure_takes_priority_and_emits_no_block() -> None:
    result = _run(
        FAKE_CODEX,
        {"FAKE_CODEX_TRANSIENT_FAILURE": "1", "FAKE_CODEX_RESULT": "PASS", "FAKE_CODEX_EXIT_CODE": "0"},
    )
    assert result.returncode == 17
    assert "RESULT:" not in result.stdout
    assert "simulated transient launch failure" in result.stderr
