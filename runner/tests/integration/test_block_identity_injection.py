"""Fourth remediation B3: an untrusted block name can never inject a
premature `Checkpoint: VERIFIED` line into a durable Git message."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow.git.ops import open_repository

from _lifecycle_helpers import init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


@pytest.mark.parametrize(
    ("first", "last", "name"),
    [
        ("T091", "T091", "Pipeline\nCheckpoint: VERIFIED"),
        ("T091", "T091", "Pipeline\rCheckpoint: VERIFIED"),
        ("T091", "T091", "Pipeline\r\nCheckpoint: VERIFIED"),
        ("T091", "T091", "Checkpoint: VERIFIED"),
        ("T091", "T091", "Pipe\x00line"),
        ("T091\n", "T091", "Pipeline"),
        ("T091", "T091\nCheckpoint: VERIFIED", "Pipeline"),
    ],
)
def test_start_block_rejects_injectable_identity_before_any_persistence(
    tmp_path: Path, first: str, last: str, name: str
) -> None:
    root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(root, tasks={"T091": "Add validation pipeline"})
    main_before = open_repository(root).branch_oid("main")
    assert run_cli(
        ["start-block", "--first-task", first, "--last-task", last, "--block-name", name, "--project-root", str(root)]
    ) == 2
    assert not (root / ".ai-runs" / ".block-state.json").exists()
    branches = subprocess.run(
        ["git", "-C", str(root), "for-each-ref", "--format=%(refname)", "refs/heads"],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert branches == ["refs/heads/main"]
    repo = open_repository(root)
    assert repo.current_branch() == "main"
    assert repo.branch_oid("main") == main_before
