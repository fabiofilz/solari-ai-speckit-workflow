"""Unit tests for item 7 (Spec Kit feature identity: canonicalized
relative to the project root, traversal-safe, symlink-safe, and
CWD-independent) - `cli.py`'s `_canonicalize_under_root` and
`_resolve_persisted_tasks_md_path`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from solari_workflow.cli import _canonicalize_under_root, _resolve_persisted_tasks_md_path
from solari_workflow.errors import SpecKitFeatureIdentityError


def test_canonicalize_returns_a_posix_relative_path(tmp_path: Path) -> None:
    feature_dir = tmp_path / "specs" / "001-demo"
    feature_dir.mkdir(parents=True)
    tasks_md = feature_dir / "tasks.md"
    tasks_md.write_text("- [ ] T001 x\n", encoding="utf-8")

    result = _canonicalize_under_root(tmp_path, tasks_md)
    assert result == "specs/001-demo/tasks.md"
    assert "\\" not in result  # always forward-slash, even conceptually on Windows


def test_canonicalize_rejects_traversal_outside_the_root(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / f"outside-{tmp_path.name}"
    outside_dir.mkdir(exist_ok=True)
    try:
        outside_file = outside_dir / "tasks.md"
        outside_file.write_text("x\n", encoding="utf-8")
        project_root = tmp_path / "project"
        project_root.mkdir()
        # A path that only reaches outside `project_root` via traversal.
        traversal_path = project_root / ".." / f"outside-{tmp_path.name}" / "tasks.md"
        with pytest.raises(SpecKitFeatureIdentityError):
            _canonicalize_under_root(project_root, traversal_path)
    finally:
        import shutil

        shutil.rmtree(outside_dir, ignore_errors=True)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink support is unreliable on Windows CI")
def test_canonicalize_rejects_a_symlink_pointing_outside_the_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_target = outside_dir / "tasks.md"
    outside_target.write_text("x\n", encoding="utf-8")

    specs_dir = project_root / "specs"
    specs_dir.mkdir()
    symlink_path = specs_dir / "tasks.md"
    symlink_path.symlink_to(outside_target)

    with pytest.raises(SpecKitFeatureIdentityError):
        _canonicalize_under_root(project_root, symlink_path)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink support is unreliable on Windows CI")
def test_canonicalize_accepts_a_symlink_pointing_inside_the_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    real_dir = project_root / "specs" / "001-real"
    real_dir.mkdir(parents=True)
    real_target = real_dir / "tasks.md"
    real_target.write_text("x\n", encoding="utf-8")

    alias_dir = project_root / "specs" / "001-alias"
    alias_dir.mkdir()
    symlink_path = alias_dir / "tasks.md"
    symlink_path.symlink_to(real_target)

    result = _canonicalize_under_root(project_root, symlink_path)
    assert result == "specs/001-real/tasks.md"  # resolved to the REAL location, not the alias


# --- _resolve_persisted_tasks_md_path: re-validated at read time ----------


def test_resolve_persisted_path_returns_none_for_none() -> None:
    assert _resolve_persisted_tasks_md_path(Path("/anywhere"), None) is None


def test_resolve_persisted_path_succeeds_for_an_existing_file(tmp_path: Path) -> None:
    feature_dir = tmp_path / "specs" / "001-demo"
    feature_dir.mkdir(parents=True)
    (feature_dir / "tasks.md").write_text("x\n", encoding="utf-8")

    result = _resolve_persisted_tasks_md_path(tmp_path, "specs/001-demo/tasks.md")
    assert result == tmp_path / "specs/001-demo/tasks.md"


def test_resolve_persisted_path_fails_closed_when_file_no_longer_exists(tmp_path: Path) -> None:
    with pytest.raises(SpecKitFeatureIdentityError):
        _resolve_persisted_tasks_md_path(tmp_path, "specs/001-demo/tasks.md")


def test_resolve_persisted_path_fails_closed_on_traversal(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    with pytest.raises(SpecKitFeatureIdentityError):
        _resolve_persisted_tasks_md_path(project_root, "../outside/tasks.md")


def test_resolve_persisted_path_is_independent_of_current_working_directory(tmp_path: Path) -> None:
    """Item 7: 'moving invocation CWD must not alter resolution' -
    resolution always uses the explicit `project_root` argument, never
    `Path.cwd()`."""
    feature_dir = tmp_path / "specs" / "001-demo"
    feature_dir.mkdir(parents=True)
    (feature_dir / "tasks.md").write_text("x\n", encoding="utf-8")

    original_cwd = os.getcwd()
    elsewhere = tmp_path.parent
    try:
        os.chdir(elsewhere)
        result = _resolve_persisted_tasks_md_path(tmp_path, "specs/001-demo/tasks.md")
        assert result == tmp_path / "specs/001-demo/tasks.md"
    finally:
        os.chdir(original_cwd)
