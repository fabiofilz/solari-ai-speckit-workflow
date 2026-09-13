"""Unit tests for `git/candidate_tree.py` (T027/T028, research.md §12).

Every test here builds its own isolated, disposable temporary Git
repository (`tmp_path` + real `git init`) — never the real project
repository — per research.md §18's "run against real `git` CLI in
temporary repositories" strategy.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from solari_workflow.git import ops
from solari_workflow.git.candidate_tree import build_candidate_tree

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "tracked.txt").write_text("original content\n", encoding="utf-8")
    (path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)


def test_deterministic_for_fixed_inputs(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    first = build_candidate_tree(repo)
    second = build_candidate_tree(repo)
    assert first.tree_oid == second.tree_oid
    assert first.head_oid == second.head_oid


def test_changes_on_tracked_file_content_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    baseline = build_candidate_tree(repo)

    (tmp_path / "tracked.txt").write_text("edited content\n", encoding="utf-8")
    edited = build_candidate_tree(repo)

    assert edited.tree_oid != baseline.tree_oid
    assert edited.head_oid == baseline.head_oid  # HEAD itself never moved


def test_changes_on_tracked_file_deletion(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    baseline = build_candidate_tree(repo)

    (tmp_path / "tracked.txt").unlink()
    deleted = build_candidate_tree(repo)

    assert deleted.tree_oid != baseline.tree_oid


def test_changes_on_a_new_untracked_and_not_ignored_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    baseline = build_candidate_tree(repo)

    (tmp_path / "new_untracked.txt").write_text("brand new\n", encoding="utf-8")
    with_new_file = build_candidate_tree(repo)

    assert with_new_file.tree_oid != baseline.tree_oid


def test_changes_on_a_file_mode_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    baseline = build_candidate_tree(repo)

    target = tmp_path / "tracked.txt"
    target.chmod(target.stat().st_mode | 0o111)  # add executable bit
    mode_changed = build_candidate_tree(repo)

    assert mode_changed.tree_oid != baseline.tree_oid


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink support is unreliable on Windows CI")
def test_changes_on_a_symlink_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    baseline = build_candidate_tree(repo)

    (tmp_path / "a_symlink").symlink_to("tracked.txt")
    with_symlink = build_candidate_tree(repo)

    assert with_symlink.tree_oid != baseline.tree_oid


def test_unaffected_by_changes_to_git_ignored_paths_including_ai_runs(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    baseline = build_candidate_tree(repo)

    ai_runs = tmp_path / ".ai-runs"
    ai_runs.mkdir()
    (ai_runs / "20260101-001-x-prompt.md").write_text("some operational content\n", encoding="utf-8")
    (ai_runs / ".block-state.json").write_text("{}", encoding="utf-8")

    with_ignored_content = build_candidate_tree(repo)

    assert with_ignored_content.tree_oid == baseline.tree_oid


def test_real_index_write_tree_oid_is_unchanged_before_and_after_a_build(tmp_path: Path) -> None:
    """The load-bearing invariant: the runner never reads or writes the
    real `.git/index` while building a Candidate Tree."""
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)

    real_index_oid_before = repo.write_tree()  # against the REAL index

    (tmp_path / "tracked.txt").write_text("changed on disk, not staged\n", encoding="utf-8")
    (tmp_path / "another_new_file.txt").write_text("also unstaged\n", encoding="utf-8")
    build_candidate_tree(repo)

    real_index_oid_after = repo.write_tree()  # against the REAL index again
    assert real_index_oid_after == real_index_oid_before


def test_real_index_remains_clean_relative_to_head_after_a_build(tmp_path: Path) -> None:
    """A stronger, complementary check: the real staging area's own
    clean-vs-HEAD precondition (research.md §0.8) is still satisfied after
    a Candidate Tree build, proving nothing leaked into the real index."""
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    assert repo.is_clean_staging_area()

    (tmp_path / "tracked.txt").write_text("changed on disk, not staged\n", encoding="utf-8")
    build_candidate_tree(repo)

    assert repo.is_clean_staging_area()


def test_temporary_index_file_is_removed_after_a_build(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    tmp_index_dir = repo.git_dir() / "solari-workflow"

    build_candidate_tree(repo)

    if tmp_index_dir.is_dir():
        assert list(tmp_index_dir.iterdir()) == []


def test_temporary_index_file_is_removed_even_when_the_build_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    tmp_index_dir = repo.git_dir() / "solari-workflow"

    original_write_tree = ops.GitRepo.write_tree

    def _boom(self: ops.GitRepo, env: dict[str, str] | None = None) -> str:
        raise RuntimeError("simulated failure during write-tree")

    monkeypatch.setattr(ops.GitRepo, "write_tree", _boom)
    with pytest.raises(RuntimeError):
        build_candidate_tree(repo)
    monkeypatch.setattr(ops.GitRepo, "write_tree", original_write_tree)

    if tmp_index_dir.is_dir():
        assert list(tmp_index_dir.iterdir()) == []


def test_does_not_require_a_clean_real_staging_area(tmp_path: Path) -> None:
    """research.md §0.8: Candidate Tree construction needs no precondition
    on the real index — a stray staged file must not prevent a build."""
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    (tmp_path / "stray.txt").write_text("staged unrelated content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "stray.txt"], check=True)

    result = build_candidate_tree(repo)  # must not raise
    assert result.tree_oid
