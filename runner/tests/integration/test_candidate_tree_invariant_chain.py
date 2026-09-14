"""Integration test (T044): each link of `Candidate Tree X -> Fingerprint
X -> staged tree X -> checkpoint commit tree X` verified independently,
end-to-end through the real CLI, plus that untracked-non-ignored files and
deletions are captured while `.ai-runs/` content is not.
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


def test_invariant_chain_links_are_all_independently_verifiable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})

    # A pre-existing tracked file this block will delete, plus a new
    # untracked-and-not-ignored file it will add - both must be captured
    # by the Candidate Tree.
    (project_root / "to_delete.txt").write_text("will be removed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project_root), "add", "to_delete.txt"], check=True)
    subprocess.run(["git", "-C", str(project_root), "commit", "-q", "-m", "seed a file to delete"], check=True)

    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0

    repo = open_repository(project_root)
    head_before_checkpoint = repo.rev_parse("HEAD")

    (project_root / "to_delete.txt").unlink()
    (project_root / "new_untracked.txt").write_text("brand new\n", encoding="utf-8")
    ai_runs_dir = project_root / ".ai-runs"
    # .ai-runs/ content must never leak into the Candidate Tree, even
    # though a real run just wrote real files into it.
    assert any(ai_runs_dir.iterdir())

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

    state = load_block_state(ai_runs_dir)
    fingerprint_x = state.last_gate
    assert fingerprint_x is not None
    assert fingerprint_x.head_oid == head_before_checkpoint  # HEAD never moved before checkpoint

    assert run_cli(["checkpoint", "--project-root", str(project_root)]) == 0

    # M6: each link of the invariant chain is asserted independently,
    # against DISTINCT Git objects - never one indirect equality chained
    # through another, and the tagged MERGE commit is never mistaken for
    # the CHECKPOINT (plumbing) commit.

    # Link 1: Candidate Tree X itself is a real, independently-inspectable
    # tree object (`git write-tree`'s own result) - not merely a value
    # copied around in Python.
    assert repo.object_type(fingerprint_x.candidate_tree_oid) == "tree"
    candidate_tree_paths = subprocess.run(
        ["git", "-C", str(project_root), "ls-tree", "-r", "--name-only", fingerprint_x.candidate_tree_oid],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "to_delete.txt" not in candidate_tree_paths
    assert "new_untracked.txt" in candidate_tree_paths
    assert not any(path.startswith(".ai-runs/") for path in candidate_tree_paths)

    # Link 2: the CHECKPOINT (plumbing) commit - reachable via the
    # DEVELOPMENT BRANCH's own ref, never via the tag (which points at a
    # separate MERGE commit, link 3 below) - has tree X.
    checkpoint_commit_oid = repo.rev_parse("T091-ValidationPipeline")
    assert repo.cat_file_tree(checkpoint_commit_oid) == fingerprint_x.candidate_tree_oid

    # Link 3: the MERGE commit - what the tag actually points at, a
    # DIFFERENT object from the checkpoint commit (it has two parents;
    # the checkpoint commit has exactly one) - independently also has
    # tree X (since `main` had not diverged).
    merge_commit_oid = repo.rev_parse("checkpoint-T091-T091^{commit}")
    assert merge_commit_oid != checkpoint_commit_oid
    assert repo.cat_file_tree(merge_commit_oid) == fingerprint_x.candidate_tree_oid

    # Link 4: the annotated tag is a genuine tag OBJECT peeling to the
    # merge commit specifically.
    assert repo.object_type("checkpoint-T091-T091") == "tag"
    assert repo.rev_parse("checkpoint-T091-T091^{commit}") == merge_commit_oid

    # The deletion and the new untracked file both made it through to the
    # final, merged `main` working directory too.
    assert not (project_root / "to_delete.txt").exists()
    assert (project_root / "new_untracked.txt").is_file()
