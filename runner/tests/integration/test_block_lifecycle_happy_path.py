"""Integration test for the full block lifecycle (T041): `start-block ->
run-claude -> run-codex-gate -> checkpoint`, against a real temporary Git
repository and the fake `claude`/`codex` CLI stubs (T026) - never a real,
paid model call.
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


def test_full_block_lifecycle_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline", "T092": "Wire it into the CLI"})

    exit_code = run_cli(
        [
            "start-block",
            "--first-task",
            "T091",
            "--last-task",
            "T092",
            "--block-name",
            "Validation Pipeline",
            "--project-root",
            str(project_root),
        ]
    )
    assert exit_code == 0

    repo = open_repository(project_root)
    assert repo.current_branch() == "T091-ValidationPipeline"

    ai_runs_dir = project_root / ".ai-runs"
    state = load_block_state(ai_runs_dir)
    assert state is not None
    assert state.state == "RUNNING"
    assert state.last_safe_stage == "BRANCH_CREATED"

    # Claude "implements" by writing a file - never touches Git.
    (project_root / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")

    exit_code = run_cli(
        [
            "run-claude",
            "--model",
            "test-model",
            "--effort",
            "high",
            "--session-mode",
            "NEW",
            "--purpose",
            "implement the validation pipeline",
            "--project-root",
            str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 0

    state = load_block_state(ai_runs_dir)
    assert state.last_safe_stage == "IMPLEMENTATION_COMPLETE"

    exit_code = run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    )
    assert exit_code == 0

    state = load_block_state(ai_runs_dir)
    assert state.last_safe_stage == "GATE_PASSED"
    assert state.last_gate is not None
    gated_tree_oid = state.last_gate.candidate_tree_oid

    exit_code = run_cli(["checkpoint", "--project-root", str(project_root)])
    assert exit_code == 0

    state = load_block_state(ai_runs_dir)
    assert state.state == "COMPLETED"
    assert state.last_safe_stage == "CHECKPOINT_COMPLETE"

    # Branch name pattern.
    assert repo.branch_exists("T091-ValidationPipeline")

    # Tag exists with the required metadata fields.
    tags = subprocess.run(
        ["git", "-C", str(project_root), "tag", "-l", "checkpoint-T091-T092"], capture_output=True, text=True, check=True
    )
    assert tags.stdout.strip() == "checkpoint-T091-T092"

    tag_message = subprocess.run(
        ["git", "-C", str(project_root), "tag", "-n99", "-l", "checkpoint-T091-T092"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Validation Pipeline" in tag_message
    assert "T091-ValidationPipeline" in tag_message
    assert "T091-T092" in tag_message
    assert "T091: Add validation pipeline" in tag_message
    assert "T092: Wire it into the CLI" in tag_message
    assert "Gate: PASS" in tag_message
    assert "Checkpoint: VERIFIED" in tag_message

    # M6: four DISTINCT objects, never conflated with one another.
    #
    # 1. The CHECKPOINT (plumbing) commit - `commit-tree`'s own result,
    #    reachable only via the DEVELOPMENT BRANCH's own ref (a plain
    #    `git merge --no-ff` never moves the branch that was merged IN,
    #    only the branch that was checked out) - NOT via the tag, which
    #    points at the MERGE commit instead (a separate object, below).
    checkpoint_commit_oid = repo.rev_parse("T091-ValidationPipeline")
    assert repo.cat_file_tree(checkpoint_commit_oid) == gated_tree_oid

    # 2. The MERGE commit - what the tag actually points at. Distinct
    #    from the checkpoint commit above: it has two parents (main's
    #    pre-merge tip and the checkpoint commit itself), while the
    #    checkpoint commit has exactly one (the original HEAD the block
    #    branch started from).
    merge_commit_oid = repo.rev_parse("checkpoint-T091-T092^{commit}")
    assert merge_commit_oid != checkpoint_commit_oid
    assert repo.cat_file_tree(merge_commit_oid) == gated_tree_oid  # no divergence -> same tree content
    parents = repo.parents_of(merge_commit_oid)
    assert len(parents) == 2
    assert parents[1] == checkpoint_commit_oid  # second parent is the merged-in branch tip

    # 3. The annotated TAG object itself - not merely a ref, a real Git
    #    object of type "tag", peeling to exactly the merge commit.
    assert repo.object_type("checkpoint-T091-T092") == "tag"

    # 4. `main`'s own ref now points at the merge commit.
    assert repo.current_branch() == "main"
    assert repo.rev_parse("main") == merge_commit_oid

    # The implemented file is present on main.
    assert (project_root / "feature.py").is_file()
