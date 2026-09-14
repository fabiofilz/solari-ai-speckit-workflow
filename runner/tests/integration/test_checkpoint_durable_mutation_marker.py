"""Fourth remediation B4: the checkpoint mutation boundary is durable
BEFORE the first real Git mutation.

    pure preflight -> write + read back BLOCKED_MANUAL /
    checkpoint_mutation_boundary="mutation_intent_recorded" -> mutate ->
    success: COMPLETED (marker replaced) | failure: marker stays (refined
    with the exact stage when that write succeeds)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from solari_workflow.git import checkpoint
from solari_workflow.git.ops import GitRepo, open_repository
from solari_workflow.state import block_state as block_state_module
from solari_workflow.state.block_state import load_block_state

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")

BRANCH = "T091-ValidationPipeline"
TAG = "checkpoint-T091-T091"
INTENT = checkpoint.STAGE_MUTATION_INTENT_RECORDED


def _passed_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    assert run_cli(
        ["run-codex-gate", "--project-root", str(root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0
    monkeypatch.undo()
    assert load_block_state(root / ".ai-runs").last_safe_stage == "GATE_PASSED"
    return root


def _state_path(root: Path) -> Path:
    return root / ".ai-runs" / ".block-state.json"


def _snapshot(root: Path) -> dict:
    repo = open_repository(root)
    return {
        "branch": repo.branch_oid(BRANCH),
        "main": repo.branch_oid("main"),
        "head_branch": repo.current_branch(),
        "index": repo.write_tree(),
        "tag": repo.tag_exists(TAG),
    }


_MUTATING_METHODS = ("read_tree", "checkout_index_all", "commit_tree", "update_ref", "switch", "merge_no_ff", "mktag")


def _record_real_mutations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy on every real-repository mutating GitRepo call (temporary-index
    calls pass an explicit `env` and are ignored)."""
    calls: list[str] = []
    for name in _MUTATING_METHODS:
        original = getattr(GitRepo, name)

        def _spy(self, *args, _name=name, _original=original, **kwargs):
            if kwargs.get("env") is None:
                calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(GitRepo, name, _spy)
    return calls


def _assert_every_lifecycle_command_refuses(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, tag_published: bool = False
) -> None:
    before = _state_path(root).read_bytes()
    gate_prompts = len(list((root / ".ai-runs").glob("*-run-codex-gate-prompt.md")))
    assert run_cli(
        ["run-codex-gate", "--project-root", str(root)], env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 2
    assert run_cli(
        ["run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW", "--purpose", "fix",
         "--project-root", str(root)],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}), monkeypatch=monkeypatch,
    ) == 2
    assert run_cli(["resume", "--project-root", str(root)]) == 2
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    assert run_cli(
        ["start-block", "--first-task", "T092", "--last-task", "T092", "--block-name", "Next Block",
         "--project-root", str(root)]
    ) == 2
    assert _state_path(root).read_bytes() == before
    assert len(list((root / ".ai-runs").glob("*-run-codex-gate-prompt.md"))) == gate_prompts
    assert open_repository(root).tag_exists(TAG) is tag_published


# 1 --------------------------------------------------------------------------


def test_marker_write_failure_means_zero_real_checkpoint_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _passed_block(tmp_path, monkeypatch)
    before_bytes = _state_path(root).read_bytes()
    before = _snapshot(root)
    original_write = block_state_module.write_block_state

    def _failing_marker_write(ai_runs_dir, state):
        if state.checkpoint_mutation_boundary == INTENT:
            raise OSError("simulated disk failure writing the pre-mutation marker")
        return original_write(ai_runs_dir, state)

    monkeypatch.setattr(block_state_module, "write_block_state", _failing_marker_write)
    calls = _record_real_mutations(monkeypatch)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    assert calls == []
    monkeypatch.undo()
    assert _snapshot(root) == before
    assert _state_path(root).read_bytes() == before_bytes  # nothing mutated, nothing to protect


def test_marker_read_back_mismatch_means_zero_real_checkpoint_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _passed_block(tmp_path, monkeypatch)
    before = _snapshot(root)
    original_load = block_state_module.load_block_state

    def _stale_read_back(ai_runs_dir):
        # The CLI's own initial load sees the real record; the marker
        # read-back sees something that is not the marker.
        state = original_load(ai_runs_dir)
        if state is not None and state.checkpoint_mutation_boundary == INTENT:
            return None
        return state

    monkeypatch.setattr(block_state_module, "load_block_state", _stale_read_back)
    calls = _record_real_mutations(monkeypatch)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    assert calls == []
    monkeypatch.undo()
    assert _snapshot(root) == before


# 2 --------------------------------------------------------------------------


def test_marker_is_on_disk_before_the_first_real_mutation_and_first_failure_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _passed_block(tmp_path, monkeypatch)
    observed: dict[str, object] = {}

    def _read_tree(self, tree_ish, env=None):
        if env is None:
            persisted = load_block_state(root / ".ai-runs")
            observed["state"] = persisted.state
            observed["boundary"] = persisted.checkpoint_mutation_boundary
            raise RuntimeError("simulated first real-index operation failure")
        return original(self, tree_ish, env=env)

    original = GitRepo.read_tree
    monkeypatch.setattr(GitRepo, "read_tree", _read_tree)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    monkeypatch.undo()
    assert observed == {"state": "BLOCKED_MANUAL", "boundary": INTENT}
    state = load_block_state(root / ".ai-runs")
    assert state.state == "BLOCKED_MANUAL"
    assert state.checkpoint_mutation_boundary == checkpoint.STAGE_INDEX_STAGING_STARTED
    _assert_every_lifecycle_command_refuses(root, monkeypatch)


# 3 --------------------------------------------------------------------------


def test_mutation_then_failed_failure_state_write_keeps_the_durable_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _passed_block(tmp_path, monkeypatch)
    original_write = block_state_module.write_block_state

    def _fail_refinement(ai_runs_dir, state):
        if state.checkpoint_mutation_boundary not in (None, INTENT):
            raise OSError("simulated failure writing the post-failure state")
        return original_write(ai_runs_dir, state)

    def _merge_boom(self, branch, message=None):
        raise RuntimeError("simulated merge failure after the branch ref advanced")

    monkeypatch.setattr(block_state_module, "write_block_state", _fail_refinement)
    monkeypatch.setattr(GitRepo, "merge_no_ff", _merge_boom)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    monkeypatch.undo()
    assert "either the durable pre-mutation marker or this refined boundary" in capsys.readouterr().err

    repo = open_repository(root)
    assert repo.cat_file_tree(repo.branch_oid(BRANCH)) is not None  # the branch ref really advanced
    state = load_block_state(root / ".ai-runs")
    assert state.state == "BLOCKED_MANUAL"
    assert state.checkpoint_mutation_boundary == INTENT
    # Operator returns to the block branch; still nothing may continue.
    import subprocess

    subprocess.run(["git", "-C", str(root), "switch", "-q", BRANCH], check=True)
    _assert_every_lifecycle_command_refuses(root, monkeypatch)


# 4 --------------------------------------------------------------------------


def test_interruption_after_marker_before_mutation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _passed_block(tmp_path, monkeypatch)
    before = _snapshot(root)
    original = GitRepo.read_tree

    def _interrupt(self, tree_ish, env=None):
        if env is None:
            raise KeyboardInterrupt  # process-like termination: not handled by the envelope
        return original(self, tree_ish, env=env)

    monkeypatch.setattr(GitRepo, "read_tree", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_cli(["checkpoint", "--project-root", str(root)])
    monkeypatch.undo()
    assert _snapshot(root) == before
    state = load_block_state(root / ".ai-runs")
    assert state.state == "BLOCKED_MANUAL"
    assert state.checkpoint_mutation_boundary == INTENT
    _assert_every_lifecycle_command_refuses(root, monkeypatch)


# 5 --------------------------------------------------------------------------


def test_successful_checkpoint_replaces_the_marker_with_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _passed_block(tmp_path, monkeypatch)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 0
    state = load_block_state(root / ".ai-runs")
    assert state.state == "COMPLETED"
    assert state.last_safe_stage == "CHECKPOINT_COMPLETE"
    assert state.checkpoint_mutation_boundary is None
    assert (root / ".ai-runs" / block_state_module.COMPLETION_RECEIPT_FILENAME).is_file()
    assert open_repository(root).tag_exists(TAG)
    # A new block may start afterwards.
    assert run_cli(
        ["start-block", "--first-task", "T092", "--last-task", "T092", "--block-name", "Next Block",
         "--project-root", str(root)]
    ) == 0


def test_successful_checkpoint_whose_final_state_write_fails_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _passed_block(tmp_path, monkeypatch)
    original_write = block_state_module.write_block_state

    def _fail_completion(ai_runs_dir, state, **kwargs):
        if state.state == "COMPLETED":
            raise OSError("simulated failure writing COMPLETED")
        return original_write(ai_runs_dir, state, **kwargs)

    monkeypatch.setattr(block_state_module, "write_block_state", _fail_completion)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    monkeypatch.undo()
    assert open_repository(root).tag_exists(TAG)
    state = load_block_state(root / ".ai-runs")
    assert state.state == "BLOCKED_MANUAL"
    assert state.checkpoint_mutation_boundary == INTENT


@pytest.mark.parametrize("failure", ["temp", "file_fsync", "replace", "directory_fsync"])
def test_final_completion_publication_failure_preserves_authoritative_interlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """The directory case reproduces the exact Codex gate adversary:
    replace succeeded, raw JSON is COMPLETED/no boundary, sync failed."""
    import errno
    import os
    import stat

    root = _passed_block(tmp_path, monkeypatch)
    ai_runs = root / ".ai-runs"
    guard_path = ai_runs / block_state_module.COMPLETION_GUARD_FILENAME
    state_path = _state_path(root)
    original_mkstemp = block_state_module.tempfile.mkstemp
    original_fsync = os.fsync
    original_replace = os.replace
    original_dir_fsync = block_state_module._fsync_directory
    events: list[str] = []

    def _during_final_state() -> bool:
        return guard_path.is_file() and not (ai_runs / block_state_module.COMPLETION_RECEIPT_FILENAME).exists()

    def _mkstemp(*args, **kwargs):
        if failure == "temp" and _during_final_state():
            events.append("temp-failed")
            raise OSError(errno.ENOSPC, "simulated final temp write failure")
        return original_mkstemp(*args, **kwargs)

    def _fsync(fd):
        if failure == "file_fsync" and _during_final_state() and stat.S_ISREG(os.fstat(fd).st_mode):
            events.append("file-fsync-failed")
            raise OSError(errno.EIO, "simulated final file fsync failure")
        return original_fsync(fd)

    def _replace(src, dst):
        if Path(dst) == state_path and _during_final_state():
            events.append("final-replace")
            if failure == "replace":
                raise OSError(errno.EIO, "simulated final replace failure")
        return original_replace(src, dst)

    def _dir_fsync(directory):
        if failure == "directory_fsync" and _during_final_state() and events == ["final-replace"]:
            events.append("directory-fsync-failed")
            raise OSError(errno.EIO, "simulated final directory fsync failure")
        return original_dir_fsync(directory)

    monkeypatch.setattr(block_state_module.tempfile, "mkstemp", _mkstemp)
    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "replace", _replace)
    monkeypatch.setattr(block_state_module, "_fsync_directory", _dir_fsync)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    monkeypatch.undo()

    assert open_repository(root).tag_exists(TAG)
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    if failure == "directory_fsync":
        assert events == ["final-replace", "directory-fsync-failed"]
        assert raw["state"] == "COMPLETED"
        assert raw["checkpoint_mutation_boundary"] is None
    else:
        assert raw["state"] == "BLOCKED_MANUAL"
    assert not (ai_runs / block_state_module.COMPLETION_RECEIPT_FILENAME).exists()
    # A fresh load models restart; every progressing command must use its
    # authoritative result, including refusing to reuse the previous PASS.
    recovered = load_block_state(ai_runs)
    assert recovered.state == "BLOCKED_MANUAL"
    assert recovered.checkpoint_mutation_boundary == INTENT
    _assert_every_lifecycle_command_refuses(root, monkeypatch, tag_published=True)


def test_interruption_after_final_replace_before_receipt_remains_blocked_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _passed_block(tmp_path, monkeypatch)
    original_write = block_state_module.write_block_state

    def _interrupt_after_replace(ai_runs_dir, state, **kwargs):
        result = original_write(ai_runs_dir, state, **kwargs)
        if state.state == "COMPLETED":
            raise KeyboardInterrupt("simulated interruption before durable publication receipt")
        return result

    monkeypatch.setattr(block_state_module, "write_block_state", _interrupt_after_replace)
    with pytest.raises(KeyboardInterrupt):
        run_cli(["checkpoint", "--project-root", str(root)])
    monkeypatch.undo()
    raw = json.loads(_state_path(root).read_text(encoding="utf-8"))
    assert raw["state"] == "COMPLETED" and raw["checkpoint_mutation_boundary"] is None
    assert load_block_state(root / ".ai-runs").checkpoint_mutation_boundary == INTENT
    _assert_every_lifecycle_command_refuses(root, monkeypatch, tag_published=True)


def test_failed_receipt_replace_cannot_publish_a_visible_completed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import os

    root = _passed_block(tmp_path, monkeypatch)
    receipt_path = root / ".ai-runs" / block_state_module.COMPLETION_RECEIPT_FILENAME
    original_replace = os.replace

    def _replace(src, dst):
        if Path(dst) == receipt_path:
            raise OSError(errno.EIO, "simulated receipt replace failure")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", _replace)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    monkeypatch.undo()
    raw = json.loads(_state_path(root).read_text(encoding="utf-8"))
    assert raw["state"] == "COMPLETED" and raw["checkpoint_mutation_boundary"] is None
    assert not receipt_path.exists()
    assert load_block_state(root / ".ai-runs").checkpoint_mutation_boundary == INTENT
    _assert_every_lifecycle_command_refuses(root, monkeypatch, tag_published=True)


# 6 --------------------------------------------------------------------------


def test_gate_identity_cannot_be_reused_while_a_marker_is_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _passed_block(tmp_path, monkeypatch)
    original = GitRepo.read_tree

    def _interrupt(self, tree_ish, env=None):
        if env is None:
            raise KeyboardInterrupt
        return original(self, tree_ish, env=env)

    monkeypatch.setattr(GitRepo, "read_tree", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_cli(["checkpoint", "--project-root", str(root)])
    monkeypatch.undo()

    # Even if the lifecycle state is flipped back to an otherwise eligible
    # value, the unresolved marker alone refuses reuse of the recorded PASS.
    data = json.loads(_state_path(root).read_text(encoding="utf-8"))
    assert data["gate_identity_record"] is not None
    data["state"] = "RUNNING"
    _state_path(root).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    before = _state_path(root).read_bytes()
    calls = _record_real_mutations(monkeypatch)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    assert calls == []
    monkeypatch.undo()
    assert run_cli(
        ["run-codex-gate", "--project-root", str(root)], env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 2
    assert _state_path(root).read_bytes() == before
    assert not open_repository(root).tag_exists(TAG)


# --- Fifth remediation B1: durability failures of the marker write --------


def _fsync_failing(monkeypatch: pytest.MonkeyPatch, *, on_directory: bool, code: int) -> None:
    import os
    import stat

    real_fsync = os.fsync

    def _fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode) == on_directory:
            raise OSError(code, os.strerror(code))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fsync)


def _checkpoint_with_failing_marker_persistence(root: Path, monkeypatch: pytest.MonkeyPatch, inject) -> list[str]:
    before = _snapshot(root)
    inject(monkeypatch)
    calls = _record_real_mutations(monkeypatch)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 2
    monkeypatch.undo()
    assert _snapshot(root) == before
    assert not open_repository(root).tag_exists(TAG)
    return calls


def test_marker_temp_file_fsync_failure_never_starts_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    root = _passed_block(tmp_path, monkeypatch)
    before_bytes = _state_path(root).read_bytes()
    calls = _checkpoint_with_failing_marker_persistence(
        root, monkeypatch, lambda mp: _fsync_failing(mp, on_directory=False, code=errno.EIO)
    )
    assert calls == []
    assert _state_path(root).read_bytes() == before_bytes


def test_marker_replace_failure_never_starts_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno
    import os

    root = _passed_block(tmp_path, monkeypatch)
    before_bytes = _state_path(root).read_bytes()

    def _inject(mp: pytest.MonkeyPatch) -> None:
        def _replace(src, dst):
            raise OSError(errno.EIO, "simulated replace failure")

        mp.setattr(os, "replace", _replace)

    calls = _checkpoint_with_failing_marker_persistence(root, monkeypatch, _inject)
    assert calls == []
    assert _state_path(root).read_bytes() == before_bytes


def test_marker_directory_fsync_eio_never_starts_mutation_and_leaves_block_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import errno

    if not block_state_module._directory_fsync_platform_supported():
        pytest.skip("directory fsync is documented as unsupported on this platform")
    root = _passed_block(tmp_path, monkeypatch)
    calls = _checkpoint_with_failing_marker_persistence(
        root, monkeypatch, lambda mp: _fsync_failing(mp, on_directory=True, code=errno.EIO)
    )
    assert calls == []
    assert "no Git mutation was started" in capsys.readouterr().err
    # The rename may already be visible but was not proven durable: the
    # visible record is the conservative marker, never an eligible PASS.
    state = load_block_state(root / ".ai-runs")
    assert state.state == "BLOCKED_MANUAL"
    assert state.checkpoint_mutation_boundary == INTENT


def test_supported_directory_fsync_accepts_the_marker_before_first_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat

    root = _passed_block(tmp_path, monkeypatch)
    events: list[str] = []
    real_fsync = os.fsync
    original_read_tree = GitRepo.read_tree

    def _fsync(fd: int) -> None:
        events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
        real_fsync(fd)

    def _read_tree(self, tree_ish, env=None):
        if env is None:
            events.append("first-real-mutation")
        return original_read_tree(self, tree_ish, env=env)

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(GitRepo, "read_tree", _read_tree)
    assert run_cli(["checkpoint", "--project-root", str(root)]) == 0
    monkeypatch.undo()
    first = events.index("first-real-mutation")
    expected = ["fsync-file", "fsync-dir"] if block_state_module._directory_fsync_platform_supported() else ["fsync-file"]
    assert events[:first] == expected
    assert load_block_state(root / ".ai-runs").state == "COMPLETED"
