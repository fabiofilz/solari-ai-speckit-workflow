"""Integration tests (T047, item 6): a simulated operational failure at
`run-claude` (attempting `IMPLEMENTATION_COMPLETE`) and at
`run-codex-gate` (attempting `GATE_PASSED`) each trigger exactly one
automatic retry and then `BLOCKED_MANUAL` if it recurs; `resume` then
revalidates and CONTINUES the block itself for every stage, INCLUDING
`BRANCH_CREATED` (item 6 remediation: `run-claude` persists the
Orchestrator-approved invocation before every attempt, so `resume`
replays it rather than needing to invent model/effort/session-mode
itself) - WITHOUT re-invoking the fake Claude stub when implementation
was already `IMPLEMENTATION_COMPLETE` (research.md §18, quickstart
Scenario 7).

Per this block's own scope boundary (see the completion report): the
single-retry policy is implemented and exercised here at the ACTOR level
(`claude`/`codex` CLI launch failures) - the two stages that involve an
external process launch, matching research.md's own retry-table
examples.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def test_run_claude_transient_failure_retries_once_then_blocked_manual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0

    exit_code = run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_TRANSIENT_FAILURE": "1"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 2

    ai_runs_dir = project_root / ".ai-runs"
    state = load_block_state(ai_runs_dir)
    assert state.state == "BLOCKED_MANUAL"
    assert state.retry_count_current_stage == 1
    assert state.last_safe_stage == "BRANCH_CREATED"  # never advanced

    # Revalidation (branch identity + HEAD) succeeds (the fixture's
    # failure is not a real Git problem), and item 6: `resume` now
    # REPLAYS the persisted, Orchestrator-approved invocation itself -
    # clearing the simulated transient condition first lets that replay
    # actually complete the implementation stage.
    exit_code = run_cli(
        ["resume", "--project-root", str(project_root)],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 0
    state = load_block_state(ai_runs_dir)
    assert state.last_safe_stage == "IMPLEMENTATION_COMPLETE"
    assert state.pending_claude_invocation is None  # cleared once COMPLETE


def test_resume_replay_can_still_fail_if_the_underlying_issue_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the Orchestrator calls `resume` before actually resolving
    whatever caused the stop, the replay can legitimately fail again -
    `resume` must not paper over that with a false success."""
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
        env=claude_env({"FAKE_CLAUDE_TRANSIENT_FAILURE": "1"}),
        monkeypatch=monkeypatch,
    ) == 2

    ai_runs_dir = project_root / ".ai-runs"
    # The underlying condition is still set (never cleared) - resume's
    # own replay must fail again, honestly.
    exit_code = run_cli(
        ["resume", "--project-root", str(project_root)],
        env=claude_env({"FAKE_CLAUDE_TRANSIENT_FAILURE": "1"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 2
    state = load_block_state(ai_runs_dir)
    assert state.state == "BLOCKED_MANUAL"
    assert state.last_safe_stage == "BRANCH_CREATED"
    assert state.pending_claude_invocation is not None  # still there to replay again later


def test_run_codex_gate_transient_failure_retries_once_then_blocked_manual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    ai_runs_dir = project_root / ".ai-runs"
    state = load_block_state(ai_runs_dir)
    assert state.last_safe_stage == "IMPLEMENTATION_COMPLETE"

    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_TRANSIENT_FAILURE": "1"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 2

    state = load_block_state(ai_runs_dir)
    assert state.state == "BLOCKED_MANUAL"
    assert state.retry_count_current_stage == 1
    assert state.last_safe_stage == "IMPLEMENTATION_COMPLETE"  # never advanced to GATE_PASSED

    # Clear the simulated transient condition and arrange for the
    # DELEGATED run-codex-gate (A3: resume executes it directly) to
    # succeed this time.
    exit_code = run_cli(
        ["resume", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 0  # resume itself ran run-codex-gate to completion
    state = load_block_state(ai_runs_dir)
    assert state.last_safe_stage == "GATE_PASSED"  # never re-invoking run-claude to get here
    assert state.last_gate is not None


def test_resume_refuses_when_state_is_not_blocked(tmp_path: Path) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0
    # state is RUNNING/BRANCH_CREATED, not a resumable state.
    exit_code = run_cli(["resume", "--project-root", str(project_root)])
    assert exit_code == 2


def test_resume_with_no_block_state_at_all_reports_manual_intervention(tmp_path: Path) -> None:
    project_root = init_repo_with_config(tmp_path)
    exit_code = run_cli(["resume", "--project-root", str(project_root)])
    assert exit_code == 2


def test_resume_after_gate_passed_revalidates_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A block stuck at GATE_PASSED (e.g. after an ARCHITECTURAL_DECISION_REQUIRED
    stop was manually cleared by re-marking state) must have its recorded
    Fingerprint revalidated by `resume`, not merely branch identity."""
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

    ai_runs_dir = project_root / ".ai-runs"
    from dataclasses import replace

    from solari_workflow.state.block_state import write_block_state

    state = load_block_state(ai_runs_dir)
    write_block_state(ai_runs_dir, replace(state, state="BLOCKED_MANUAL"))

    # Mutate the working tree so the recorded Fingerprint no longer matches.
    (project_root / "feature.py").write_text("def feature():\n    return 43  # mutated\n", encoding="utf-8")

    exit_code = run_cli(["resume", "--project-root", str(project_root)])
    assert exit_code == 1  # revalidation failed
    state = load_block_state(ai_runs_dir)
    assert state.state == "BLOCKED_MANUAL"  # unchanged, never silently advanced


def test_resume_from_gate_passed_actually_completes_the_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A3: resume does not merely REPORT that 'checkpoint' is next when
    last_safe_stage is GATE_PASSED - it executes it, going through the
    exact same `_cmd_checkpoint` code path a direct Orchestrator
    invocation would."""
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

    ai_runs_dir = project_root / ".ai-runs"
    from dataclasses import replace

    from solari_workflow.state.block_state import write_block_state

    state = load_block_state(ai_runs_dir)
    write_block_state(ai_runs_dir, replace(state, state="BLOCKED_MANUAL"))

    exit_code = run_cli(["resume", "--project-root", str(project_root)])
    assert exit_code == 0
    state = load_block_state(ai_runs_dir)
    assert state.state == "COMPLETED"
    assert state.last_safe_stage == "CHECKPOINT_COMPLETE"
