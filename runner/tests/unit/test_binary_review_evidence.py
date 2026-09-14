"""Third remediation M7: machine-safe binary review evidence.

`GitRepo.diff_numstat` runs with rename detection disabled and NUL
delimiting, so every binary path the evidence names is a literal path in
Candidate Tree X (or, for a deletion, at HEAD) - never a rename display
string such as `old => new` or `{a => b}/c`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow.cli import _describe_binary_evidence
from solari_workflow.fingerprint.engine import compute_fingerprint
from solari_workflow.git.candidate_tree import build_candidate_tree
from solari_workflow.git.ops import open_repository

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")

BINARY_A = bytes(range(256)) * 8
BINARY_B = bytes(reversed(range(256))) * 8


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True)


def _repo_with_seed(tmp_path: Path, files: dict[str, bytes]):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return open_repository(tmp_path)


def _evidence(repo) -> tuple[str, dict[str, tuple[str, str, str]]]:
    candidate = build_candidate_tree(repo)
    fingerprint = compute_fingerprint(repo, candidate, first_task="T091", last_task="T091")
    return _describe_binary_evidence(repo, fingerprint), repo.ls_tree_entries(candidate.tree_oid)


def _entry_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("  - ")]


def test_pure_binary_rename_is_new_path_addition_plus_old_path_deletion(tmp_path: Path) -> None:
    repo = _repo_with_seed(tmp_path, {"assets/old name.bin": BINARY_A})
    (tmp_path / "assets" / "old name.bin").rename(tmp_path / "assets" / "new name.bin")
    text, entries = _evidence(repo)
    lines = _entry_lines(text)
    assert "=>" not in text and "{" not in text
    new_oid = entries["assets/new name.bin"][2]
    assert any(line.startswith("  - assets/new name.bin: binary blob") and new_oid in line for line in lines)
    assert "  - assets/old name.bin: binary file DELETED relative to HEAD (absent from Candidate Tree X)" in lines
    assert len(lines) == 2


def test_binary_rename_with_content_change_binds_the_new_blob(tmp_path: Path) -> None:
    repo = _repo_with_seed(tmp_path, {"a.bin": BINARY_A})
    (tmp_path / "a.bin").unlink()
    (tmp_path / "b.bin").write_bytes(BINARY_B)
    text, entries = _evidence(repo)
    lines = _entry_lines(text)
    assert "=>" not in text
    assert any(line.startswith("  - b.bin:") and entries["b.bin"][2] in line and f"{len(BINARY_B)} bytes" in line for line in lines)
    assert any(line.startswith("  - a.bin:") and "DELETED" in line for line in lines)
    assert len(lines) == 2


def test_binary_add_modify_delete(tmp_path: Path) -> None:
    repo = _repo_with_seed(tmp_path, {"mod.bin": BINARY_A, "del.bin": BINARY_A})
    (tmp_path / "add.bin").write_bytes(BINARY_B)
    (tmp_path / "mod.bin").write_bytes(BINARY_B)
    (tmp_path / "del.bin").unlink()
    text, entries = _evidence(repo)
    lines = _entry_lines(text)
    assert len(lines) == 3
    for path in ("add.bin", "mod.bin"):
        mode, obj_type, oid = entries[path]
        assert any(
            line.startswith(f"  - {path}: binary {obj_type}, mode {mode}, candidate-tree blob oid {oid}") for line in lines
        )
    assert "del.bin" not in entries
    assert any(line.startswith("  - del.bin:") and "DELETED" in line for line in lines)
    # Raw bytes are never embedded.
    assert BINARY_B[:32].decode("latin-1") not in text


def test_diff_numstat_is_rename_free_and_nul_safe(tmp_path: Path) -> None:
    repo = _repo_with_seed(tmp_path, {"x => y.bin": BINARY_A})
    (tmp_path / "x => y.bin").rename(tmp_path / "tab\tname.bin")
    candidate = build_candidate_tree(repo)
    head = repo.rev_parse("HEAD")
    records = repo.diff_numstat(head, candidate.tree_oid)
    assert sorted(path for _, _, path in records) == ["tab\tname.bin", "x => y.bin"]
    assert all(added is None and deleted is None for added, deleted, _ in records)
