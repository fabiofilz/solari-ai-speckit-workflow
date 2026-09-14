"""Fifth remediation B2: an unresolved checkpoint mutation boundary takes
precedence over the mutable lifecycle `state` in every workflow-progressing
command, including `start-block`."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow import cli
from solari_workflow.git.ops import GitRepo, open_repository

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")

STATES = ["COMPLETED", "RUNNING", "READY_TO_RESUME", "QA_REMEDIATION_REQUIRED", "ARCHITECTURAL_DECISION_REQUIRED",
          "BLOCKED_MANUAL", "RETRYABLE_ERROR"]
BOUNDARY_MESSAGE = "unresolved checkpoint mutation boundary"


def _gated_block_with_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_value: str) -> Path:
    root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(root, tasks={"T091": "Add validation pipeline", "T092": "Next"})
    assert run_cli(
        ["start-block", "--first-task", "T091", "--last-task", "T091", "--block-name", "Validation Pipeline",
         "--project-root", str(root)]
    ) == 0
    (root / "feature.py").write_text("x = 1\n", encoding="utf-8")
    assert run_cli(
        ["run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW", "--purpose", "implement",
         "--project-root", str(root)],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}), monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(
        ["run-codex-gate", "--project-root", str(root)], env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    monkeypatch.undo()
    path = root / ".ai-runs" / ".block-state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["state"] = state_value
    data["checkpoint_mutation_boundary"] = "merge_attempted"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return root


def _heads(root: Path) -> list[str]:
    return subprocess.run(
        ["git", "-C", str(root), "for-each-ref", "--format=%(refname)", "refs/heads"],
        check=True, capture_output=True, text=True,
    ).stdout.split()


@pytest.mark.parametrize("state_value", STATES)
def test_every_progressing_command_refuses_on_boundary_regardless_of_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state_value: str
) -> None:
    root = _gated_block_with_boundary(tmp_path, monkeypatch, state_value)
    state_path = root / ".ai-runs" / ".block-state.json"
    before = state_path.read_bytes()
    heads_before = _heads(root)
    files_before = sorted(p.name for p in (root / ".ai-runs").iterdir())
    repo = open_repository(root)
    snapshot = (repo.branch_oid("main"), repo.current_branch(), repo.write_tree())
    capsys.readouterr()

    def _no_actor(*args, **kwargs):
        raise AssertionError("no actor may run while a mutation boundary is unresolved")

    def _no_git_mutation(*args, **kwargs):
        raise AssertionError("no branch/checkpoint Git mutation may start while a boundary is unresolved")

    monkeypatch.setattr(cli.claude_actor, "run_claude_session", _no_actor)
    monkeypatch.setattr(cli.codex_actor, "run_codex_gate_session", _no_actor)
    for name in ("create_branch", "switch", "read_tree", "commit_tree", "update_ref", "merge_no_ff", "mktag"):
        original = getattr(GitRepo, name)

        def _guard(self, *args, _original=original, **kwargs):
            if kwargs.get("env") is not None:  # temporary-index candidate-tree work only
                return _original(self, *args, **kwargs)
            return _no_git_mutation()

        monkeypatch.setattr(GitRepo, name, _guard)

    commands = [
        (["start-block", "--first-task", "T092", "--last-task", "T092", "--block-name", "Next Block"], None),
        (["run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW", "--purpose", "fix"],
         claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"})),
        (["run-codex-gate"], codex_env({"FAKE_CODEX_RESULT": "PASS"})),
        (["checkpoint"], None),
        (["resume"], claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"})),
    ]
    for argv, env in commands:
        exit_code = run_cli(argv + ["--project-root", str(root)], env=env, monkeypatch=monkeypatch if env else None)
        err = capsys.readouterr().err
        assert exit_code == 2, argv
        assert BOUNDARY_MESSAGE in err, (argv, err)
        assert f"{argv[0]} refused" in err
        assert state_path.read_bytes() == before, argv

    assert _heads(root) == heads_before
    assert sorted(p.name for p in (root / ".ai-runs").iterdir()) == files_before
    monkeypatch.undo()
    repo = open_repository(root)
    assert (repo.branch_oid("main"), repo.current_branch(), repo.write_tree()) == snapshot
    assert not repo.tag_exists("checkpoint-T091-T091")


def test_start_block_refuses_before_readiness_when_boundary_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _gated_block_with_boundary(tmp_path, monkeypatch, "COMPLETED")

    def _no_readiness(*args, **kwargs):
        raise AssertionError("readiness must not run before the boundary interlock")

    monkeypatch.setattr(cli, "run_readiness_gate", _no_readiness)
    assert run_cli(
        ["start-block", "--first-task", "T092", "--last-task", "T092", "--block-name", "Next Block",
         "--project-root", str(root)]
    ) == 2
