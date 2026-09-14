"""Third remediation regression tests driven through the real CLI:

- B1 latest gate ATTEMPT supersedes any earlier PASS, including when the
  new attempt dies with an unexpected (non-transient) exception;
- B2 `run-codex-gate` never starts from `BLOCKED_MANUAL`, in particular the
  exact partial-checkpoint scenario;
- M1 base binding before a gate may become eligible;
- M4 the partial-failure boundary is persisted and blocks continuation;
- M6 `resume` on ARCHITECTURAL_DECISION_REQUIRED refuses without writes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow import cli
from solari_workflow.git.ops import GitRepo, open_repository
from solari_workflow.state import gate_identity as gate_identity_mod
from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")

BRANCH = "T091-ValidationPipeline"
TAG = "checkpoint-T091-T091"


def _implemented_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    return project_root


def _gate(project_root: Path, monkeypatch: pytest.MonkeyPatch, result: str = "PASS") -> int:
    return run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": result}),
        monkeypatch=monkeypatch,
    )


def _passed_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project_root = _implemented_block(tmp_path, monkeypatch)
    assert _gate(project_root, monkeypatch) == 0
    state = load_block_state(project_root / ".ai-runs")
    assert state.last_safe_stage == "GATE_PASSED"
    return project_root


def _state_bytes(project_root: Path) -> bytes:
    return (project_root / ".ai-runs" / ".block-state.json").read_bytes()


def _gate_prompt_count(project_root: Path) -> int:
    return len(list((project_root / ".ai-runs").glob("*-run-codex-gate-prompt.md")))


def _assert_not_checkpoint_eligible(project_root: Path) -> None:
    state = load_block_state(project_root / ".ai-runs")
    assert state.last_safe_stage != "GATE_PASSED"
    assert state.last_gate is None
    assert state.gated_main_oid is None
    assert state.gate_feature_identity is None
    assert state.gate_identity_record is None
    assert state.gate_identity_sha256 is None
    repo = open_repository(project_root)
    main_before = repo.rev_parse("main")
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) != 0
    assert not repo.tag_exists(TAG)
    assert repo.rev_parse("main") == main_before
    assert load_block_state(project_root / ".ai-runs").state != "COMPLETED"


# --- B1 ----------------------------------------------------------------------


def test_b1_unexpected_exception_while_building_the_second_gate_revokes_the_first_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = _passed_block(tmp_path, monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected failure outside the transient path")

    monkeypatch.setattr(cli, "_build_codex_review_prompt", _boom)
    assert _gate(project_root, monkeypatch) == 2
    monkeypatch.undo()
    _assert_not_checkpoint_eligible(project_root)


def test_b1_evidence_write_failure_after_a_pass_verdict_is_never_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = _passed_block(tmp_path, monkeypatch)

    def _boom(*args, **kwargs):
        raise OSError("simulated evidence write failure")

    monkeypatch.setattr(gate_identity_mod, "write_gate_evidence", _boom)
    assert _gate(project_root, monkeypatch) == 2
    monkeypatch.undo()
    _assert_not_checkpoint_eligible(project_root)


def test_b1_refusal_on_wrong_branch_after_a_pass_still_revokes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = _passed_block(tmp_path, monkeypatch)
    subprocess.run(["git", "-C", str(project_root), "switch", "-q", "main"], check=True)
    assert _gate(project_root, monkeypatch) == 2
    subprocess.run(["git", "-C", str(project_root), "switch", "-q", BRANCH], check=True)
    _assert_not_checkpoint_eligible(project_root)


# --- B2 / M4 -----------------------------------------------------------------


def test_b2_partial_checkpoint_failure_blocks_every_automatic_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project_root = _passed_block(tmp_path, monkeypatch)
    repo = open_repository(project_root)

    def _merge_boom(self: GitRepo, branch: str, message: str | None = None) -> None:
        raise RuntimeError("simulated merge failure")

    monkeypatch.setattr(GitRepo, "merge_no_ff", _merge_boom)
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 2
    monkeypatch.undo()
    err = capsys.readouterr().err
    assert "'merge_attempted'" in err
    assert "MAY be modified" in err

    state = load_block_state(project_root / ".ai-runs")
    assert state.state == "BLOCKED_MANUAL"
    assert state.checkpoint_mutation_boundary == "merge_attempted"
    assert not repo.tag_exists(TAG)

    # Exact scenario: operator switches back to the block branch, then a
    # direct gate is attempted. It must refuse and change nothing.
    subprocess.run(["git", "-C", str(project_root), "switch", "-q", BRANCH], check=True)
    before = _state_bytes(project_root)
    prompts_before = _gate_prompt_count(project_root)
    assert _gate(project_root, monkeypatch) == 2
    assert "BLOCKED_MANUAL" in capsys.readouterr().err
    assert _state_bytes(project_root) == before
    assert _gate_prompt_count(project_root) == prompts_before

    assert run_cli(["resume", "--project-root", str(project_root)]) == 2
    assert _state_bytes(project_root) == before
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 2
    assert _state_bytes(project_root) == before
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "fix", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 2
    assert _state_bytes(project_root) == before
    assert not repo.tag_exists(TAG)


def test_b2_blocked_manual_without_a_boundary_is_also_refused_by_direct_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = _implemented_block(tmp_path, monkeypatch)
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_TRANSIENT_FAILURE": "1"}),
        monkeypatch=monkeypatch,
    ) == 2
    assert load_block_state(project_root / ".ai-runs").state == "BLOCKED_MANUAL"
    before = _state_bytes(project_root)
    prompts_before = _gate_prompt_count(project_root)
    assert _gate(project_root, monkeypatch) == 2
    assert _state_bytes(project_root) == before
    assert _gate_prompt_count(project_root) == prompts_before


def test_m4_unverified_tag_object_boundary_is_persisted_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project_root = _passed_block(tmp_path, monkeypatch)
    original = GitRepo.read_tag_object

    def _tampered(self: GitRepo, oid: str) -> str:
        return original(self, oid) + "extra\n"

    monkeypatch.setattr(GitRepo, "read_tag_object", _tampered)
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 2
    monkeypatch.undo()
    err = capsys.readouterr().err
    assert "'tag_object_created'" in err
    assert "fully-verified" not in err
    state = load_block_state(project_root / ".ai-runs")
    assert state.state == "BLOCKED_MANUAL"
    assert state.checkpoint_mutation_boundary == "tag_object_created"
    assert not open_repository(project_root).tag_exists(TAG)


# --- M1 ----------------------------------------------------------------------


def test_m1_gate_refuses_when_main_advanced_before_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = _implemented_block(tmp_path, monkeypatch)
    subprocess.run(["git", "-C", str(project_root), "stash", "-q", "-u"], check=True)
    subprocess.run(["git", "-C", str(project_root), "switch", "-q", "main"], check=True)
    subprocess.run(["git", "-C", str(project_root), "commit", "-q", "--allow-empty", "-m", "advance"], check=True)
    subprocess.run(["git", "-C", str(project_root), "switch", "-q", BRANCH], check=True)
    subprocess.run(["git", "-C", str(project_root), "stash", "pop", "-q"], check=True)
    prompts_before = _gate_prompt_count(project_root)
    assert _gate(project_root, monkeypatch) == 2
    assert _gate_prompt_count(project_root) == prompts_before  # no actor invoked
    _assert_not_checkpoint_eligible(project_root)


def test_m1_gate_refuses_when_block_head_differs_from_expected_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = _implemented_block(tmp_path, monkeypatch)
    subprocess.run(["git", "-C", str(project_root), "commit", "-q", "--allow-empty", "-m", "rogue"], check=True)
    prompts_before = _gate_prompt_count(project_root)
    assert _gate(project_root, monkeypatch) == 2
    assert _gate_prompt_count(project_root) == prompts_before
    _assert_not_checkpoint_eligible(project_root)


def test_m1_all_bases_equal_gate_and_checkpoint_proceed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = _passed_block(tmp_path, monkeypatch)
    state = load_block_state(project_root / ".ai-runs")
    assert state.last_gate.head_oid == state.expected_head_oid == state.gated_main_oid
    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 0
    assert open_repository(project_root).tag_exists(TAG)


# --- M6 ----------------------------------------------------------------------


def test_m6_resume_on_architectural_decision_required_refuses_without_writes(
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
    run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "ARCHITECTURAL_DECISION_REQUIRED"}),
        monkeypatch=monkeypatch,
    )
    assert load_block_state(project_root / ".ai-runs").state == "ARCHITECTURAL_DECISION_REQUIRED"

    ai_runs = project_root / ".ai-runs"
    before = _state_bytes(project_root)
    files_before = sorted(p.name for p in ai_runs.iterdir())

    def _no_actor(*args, **kwargs):
        raise AssertionError("resume must never invoke an actor for ARCHITECTURAL_DECISION_REQUIRED")

    monkeypatch.setattr(cli.claude_actor, "run_claude_session", _no_actor)
    monkeypatch.setattr(cli.codex_actor, "run_codex_gate_session", _no_actor)
    assert run_cli(
        ["resume", "--project-root", str(project_root)],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 2
    assert _state_bytes(project_root) == before
    assert sorted(p.name for p in ai_runs.iterdir()) == files_before
