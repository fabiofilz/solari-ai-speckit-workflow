"""Fourth remediation B2: the configured main branch identity is
`refs/heads/<main>`, never a short revision name a same-named tag could
win in Git's ref disambiguation (`refs/tags/<name>` is tried before
`refs/heads/<name>`).

Scenario: branch refs/heads/main at B, a tag named `main` at A, the
block's expected base A. The workflow must observe branch main == B and
refuse - no actor runs, no checkpoint mutation starts.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow.git.ops import open_repository
from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")

BRANCH = "T091-ValidationPipeline"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _advance_main_branch_and_tag_old_main(root: Path, tag_kind: str) -> tuple[str, str]:
    """Move refs/heads/main from A to a new commit B (without touching the
    worktree/index) and create a tag named `main` pointing at A."""
    a = _git(root, "rev-parse", "refs/heads/main")
    tree = _git(root, "rev-parse", "refs/heads/main^{tree}")
    b = _git(root, "commit-tree", tree, "-p", a, "-m", "main advanced")
    _git(root, "update-ref", "refs/heads/main", b, a)
    if tag_kind == "lightweight":
        _git(root, "update-ref", "refs/tags/main", a)
    else:
        _git(root, "tag", "-a", "-m", "ambiguous", "main", a)
    return a, b


def _start_and_implement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        ["start-block", "--first-task", "T091", "--last-task", "T091", "--block-name", "Validation Pipeline",
         "--project-root", str(root)]
    ) == 0
    (root / "feature.py").write_text("x = 1\n", encoding="utf-8")
    assert run_cli(
        ["run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW", "--purpose", "implement",
         "--project-root", str(root)],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0
    return root


@pytest.mark.parametrize("tag_kind", ["lightweight", "annotated"])
def test_gate_observes_branch_main_not_a_same_named_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag_kind: str
) -> None:
    root = _start_and_implement(tmp_path, monkeypatch)
    expected = load_block_state(root / ".ai-runs").expected_head_oid
    a, b = _advance_main_branch_and_tag_old_main(root, tag_kind)
    assert expected == a and a != b

    prompts_before = len(list((root / ".ai-runs").glob("*-run-codex-gate-prompt.md")))
    assert run_cli(
        ["run-codex-gate", "--project-root", str(root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 2
    assert len(list((root / ".ai-runs").glob("*-run-codex-gate-prompt.md"))) == prompts_before  # actor never ran
    state = load_block_state(root / ".ai-runs")
    assert state.last_safe_stage != "GATE_PASSED"
    assert state.gated_main_oid is None


@pytest.mark.parametrize("tag_kind", ["lightweight", "annotated"])
def test_checkpoint_observes_branch_main_not_a_same_named_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag_kind: str
) -> None:
    root = _start_and_implement(tmp_path, monkeypatch)
    assert run_cli(
        ["run-codex-gate", "--project-root", str(root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    gated = load_block_state(root / ".ai-runs").gated_main_oid
    a, b = _advance_main_branch_and_tag_old_main(root, tag_kind)
    assert gated == a

    repo = open_repository(root)
    branch_before = repo.branch_oid(BRANCH)
    index_before = repo.write_tree()
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 1
    state = load_block_state(root / ".ai-runs")
    assert state.checkpoint_mutation_boundary is None
    assert state.state != "COMPLETED"
    assert repo.branch_oid(BRANCH) == branch_before
    assert repo.branch_oid("main") == b
    assert repo.write_tree() == index_before
    assert repo.current_branch() == BRANCH
    assert not repo.tag_exists("checkpoint-T091-T091")


def test_start_block_bases_on_branch_main_not_a_same_named_tag(tmp_path: Path) -> None:
    root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(root, tasks={"T091": "Add validation pipeline"})
    a, b = _advance_main_branch_and_tag_old_main(root, "lightweight")
    assert run_cli(
        ["start-block", "--first-task", "T091", "--last-task", "T091", "--block-name", "Validation Pipeline",
         "--project-root", str(root)]
    ) == 0
    state = load_block_state(root / ".ai-runs")
    assert state.expected_head_oid == b
    repo = open_repository(root)
    assert repo.current_branch() == BRANCH
    assert repo.rev_parse("HEAD") == b


def test_current_branch_is_exact_even_when_a_tag_shares_its_name(tmp_path: Path) -> None:
    root = init_repo_with_config(tmp_path)
    _git(root, "update-ref", "refs/tags/main", _git(root, "rev-parse", "refs/heads/main"))
    assert open_repository(root).current_branch() == "main"
