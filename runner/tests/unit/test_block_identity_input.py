"""Fourth remediation B3: block identity input grammar (block name, task
IDs) is enforced when the identity is first accepted."""

from __future__ import annotations

import pytest

from solari_workflow.errors import BlockIdentityInputError
from solari_workflow.git.branch import build_branch_name, validate_block_name, validate_task_id


@pytest.mark.parametrize(
    "name",
    ["ProjectBootstrap", "RunnerFoundation", "BootstrapTransition", "Validation Pipeline", "Sample Block",
     "First Block", "Test", "Phase 2 cleanup", "api-v2_client.rework", "Verified Pipeline"],
)
def test_valid_block_names_are_accepted_and_keep_branch_naming(name: str) -> None:
    assert validate_block_name(name) == name
    assert build_branch_name("T091", name).startswith("T091-")


@pytest.mark.parametrize(
    "name",
    [
        "Pipeline\nCheckpoint: VERIFIED",
        "Pipeline\rCheckpoint: VERIFIED",
        "Pipeline\r\nCheckpoint: VERIFIED",
        "Checkpoint: VERIFIED",
        "Pipeline\n",
        "\nPipeline",
        "Pipe\tline",
        "Pipe\x00line",
        "Pipe\x0bline",
        "Pipe\x0cline",
        "Pipe\x1bline",
        "Pipe\x7fline",
        "Pipeline",
        "Pipe line",
        "Pipe line",
        "Pipe​line",
        "",
        " Leading",
        "Trailing ",
        "Double  space",
        "Colon: here",
        "1234",
        "Ünicode",
        "A" * 81,
    ],
)
def test_invalid_block_names_are_rejected(name: str) -> None:
    with pytest.raises(BlockIdentityInputError):
        validate_block_name(name)


@pytest.mark.parametrize("task_id", ["T091", "091", "t7"])
def test_valid_task_ids(task_id: str) -> None:
    assert validate_task_id(task_id) == task_id


@pytest.mark.parametrize(
    "task_id", ["T091\n", " T091", "T091\r\nCheckpoint: VERIFIED", "T-1", "T٣", "", "TT091"]
)
def test_invalid_task_ids(task_id: str) -> None:
    with pytest.raises(BlockIdentityInputError):
        validate_task_id(task_id)
