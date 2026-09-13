"""Unit tests for `git/ops.py` repository detection and staged-state inspection.

Every test here creates its own isolated, disposable temporary Git
repository (`tempfile`/`tmp_path` + `git init`) — never the real project
repository — per research.md §18's "run against real `git` CLI in
temporary repositories" testing strategy.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow.errors import GitAmbiguityError, StagingPreconditionError
from solari_workflow.git import ops
from solari_workflow.platform import proc as proc_module

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)


def test_is_git_repository_true_inside_a_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert ops.is_git_repository(tmp_path) is True


def test_is_git_repository_false_outside_a_repo(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    assert ops.is_git_repository(plain_dir) is False


def test_open_repository_raises_git_ambiguity_error_when_absent(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    with pytest.raises(GitAmbiguityError):
        ops.open_repository(plain_dir)


# --- Repository-detection fail-closed semantics (3rd re-gate findings 2, 3 & 4) ---
#
# Classification is now filesystem-based (does `.git` metadata exist
# anywhere at or above the candidate path?), not derived from Git's own
# stderr text — stderr can be locale-translated, and the exact same
# English phrase covers both genuine absence AND a corrupted `.git`
# pointer, so it can never be the semantic source of truth. These tests
# use real disposable repos/paths for the filesystem-truth cases and
# monkeypatch `proc.run` at the `git/ops.py` call site only to prove the
# classification does NOT depend on stderr content/language — never by
# running Git against the real project repository.


def _fake_run_returning(returncode: int, stderr: str):
    def _fake_run(argv, *, cwd=None, env=None, check=False):  # noqa: ARG001 - test double
        return proc_module.ProcessResult(returncode=returncode, stdout="", stderr=stderr, duration=0.0)

    return _fake_run


def _make_separate_git_dir_worktree(tmp_path: Path) -> Path:
    """A repo whose `.git` is a FILE (`gitdir: ...`) pointing at a real,
    separate Git directory — the same on-disk shape a `git worktree add`
    or submodule checkout produces (`git init --separate-git-dir`)."""
    worktree = tmp_path / "worktree-style"
    external_gitdir = tmp_path / "external-gitdir"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", f"--separate-git-dir={external_gitdir}", str(worktree)],
        check=True,
    )
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True)
    (worktree / "f.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-q", "-m", "initial"], check=True)
    assert (worktree / ".git").is_file()  # sanity: really is a `.git` FILE, not a directory
    return worktree


def test_is_git_repository_true_for_a_separate_git_dir_worktree_style_repo(tmp_path: Path) -> None:
    worktree = _make_separate_git_dir_worktree(tmp_path)
    assert ops.is_git_repository(worktree) is True


def test_open_repository_resolves_a_separate_git_dir_worktree_style_repo(tmp_path: Path) -> None:
    worktree = _make_separate_git_dir_worktree(tmp_path)
    repo = ops.open_repository(worktree)
    assert repo.path.resolve() == worktree.resolve()


def test_is_git_repository_raises_for_a_broken_git_file_pointer(tmp_path: Path) -> None:
    """Reproduces the exact Codex scenario: a `.git` FILE whose `gitdir:`
    target does not exist. Real Git emits `fatal: not a git repository:
    (null)` here — the SAME phrase it uses for genuine absence — so
    stderr text alone cannot tell these apart; the filesystem fact that
    `.git` exists at this exact path is what must drive the answer."""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /definitely/does/not/exist/at/all\n", encoding="utf-8")
    with pytest.raises(GitAmbiguityError) as excinfo:
        ops.is_git_repository(broken)
    assert str(broken) in str(excinfo.value) or "resolve" in str(excinfo.value).lower()


def test_open_repository_raises_for_a_broken_git_file_pointer_not_as_mere_absence(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /definitely/does/not/exist/at/all\n", encoding="utf-8")
    with pytest.raises(GitAmbiguityError) as excinfo:
        ops.open_repository(broken)
    message = str(excinfo.value).lower()
    assert "not present" not in message  # this is corruption, not absence
    assert "could not resolve" in message


def test_is_git_repository_raises_for_a_malformed_empty_git_directory(tmp_path: Path) -> None:
    """An empty `.git` DIRECTORY (no valid internal structure) makes real
    Git emit the exact same "not a git repository (or any of the parent
    directories)" wording as genuine absence — again proving stderr text
    cannot be the discriminator; `.git` existing at this path must be."""
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / ".git").mkdir()
    with pytest.raises(GitAmbiguityError):
        ops.is_git_repository(malformed)


def test_open_repository_raises_for_a_malformed_empty_git_directory(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / ".git").mkdir()
    with pytest.raises(GitAmbiguityError):
        ops.open_repository(malformed)


def test_is_git_repository_raises_for_a_missing_working_directory(tmp_path: Path) -> None:
    """The requested path does not exist at all — must be a structured
    `GitAmbiguityError`, never a raw `FileNotFoundError`/`NotADirectoryError`
    escaping to the caller (finding 3)."""
    missing = tmp_path / "does-not-exist-at-all"
    with pytest.raises(GitAmbiguityError) as excinfo:
        ops.is_git_repository(missing)
    assert str(missing) in str(excinfo.value)


def test_open_repository_raises_for_a_missing_working_directory(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist-at-all"
    with pytest.raises(GitAmbiguityError) as excinfo:
        ops.open_repository(missing)
    assert str(missing) in str(excinfo.value)


def test_is_git_repository_raises_for_a_working_directory_that_is_a_plain_file(tmp_path: Path) -> None:
    """`cwd` existing but not being a directory (`NotADirectoryError`) is
    the other raw-OSError shape `subprocess` can raise for a bad `cwd`."""
    a_file = tmp_path / "just-a-file"
    a_file.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(GitAmbiguityError):
        ops.is_git_repository(a_file)


def test_is_git_repository_raises_on_an_unexpected_process_level_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `OSError` raised by the subprocess layer itself (git failing to
    even launch — e.g. a permission error executing the binary) must
    become a structured `GitAmbiguityError`, regardless of `.git`
    metadata, since it never even produced a Git-level answer to
    classify."""

    def _fake_run(argv, *, cwd=None, env=None, check=False):  # noqa: ARG001 - test double
        raise PermissionError("simulated: git binary not executable")

    monkeypatch.setattr(ops.proc, "run", _fake_run)
    with pytest.raises(GitAmbiguityError) as excinfo:
        ops.is_git_repository(tmp_path)
    assert "simulated" in str(excinfo.value)


def test_is_git_repository_raises_on_injected_io_failure_when_git_metadata_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected Git-level I/O failure (nonzero exit, an I/O-error-
    shaped message) while real `.git` metadata is present at the path
    must be treated as corruption/ambiguity, never absence."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        ops.proc, "run", _fake_run_returning(128, "fatal: unable to read current working directory: I/O error")
    )
    with pytest.raises(GitAmbiguityError) as excinfo:
        ops.is_git_repository(tmp_path)
    assert "I/O error" in str(excinfo.value)


def test_is_git_repository_absence_classification_ignores_translated_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for finding 4: a plain directory with NO `.git`
    metadata anywhere must still correctly classify as `False`, even
    when Git's (simulated, here non-English) diagnostic text does not
    contain any recognizable English "not a git repository" phrase at
    all — classification must come from filesystem state, not stderr
    language."""
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    monkeypatch.setattr(
        ops.proc,
        "run",
        _fake_run_returning(128, "fatal : ceci n'est pas un dépôt Git (ni aucun des répertoires parents) : .git"),
    )
    assert ops.is_git_repository(plain_dir) is False


def test_open_repository_absence_classification_ignores_translated_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    monkeypatch.setattr(
        ops.proc,
        "run",
        _fake_run_returning(128, "fatal : ceci n'est pas un dépôt Git (ni aucun des répertoires parents) : .git"),
    )
    with pytest.raises(GitAmbiguityError) as excinfo:
        ops.open_repository(plain_dir)
    assert "not present" in str(excinfo.value).lower()


def test_git_metadata_exists_anywhere_finds_a_git_marker_in_an_ancestor_directory(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert ops._git_metadata_exists_anywhere(nested) is True


def test_git_metadata_exists_anywhere_false_when_none_exists_up_to_the_root(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert ops._git_metadata_exists_anywhere(nested) is False


def test_git_metadata_exists_anywhere_fails_toward_true_on_a_filesystem_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe failure (permission denied, a race, ...) must never be
    silently treated as "definitely absent" — it fails toward `True` so
    the caller escalates to `GitAmbiguityError` instead of guessing."""

    def _boom(self: Path) -> bool:
        raise PermissionError("simulated: cannot stat")

    monkeypatch.setattr(Path, "exists", _boom)
    assert ops._git_metadata_exists_anywhere(tmp_path) is True


def test_open_repository_absence_message_names_repository_not_present(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    with pytest.raises(GitAmbiguityError) as excinfo:
        ops.open_repository(plain_dir)
    message = str(excinfo.value).lower()
    assert "not present" in message


def test_open_repository_resolves_to_the_repo_root(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    repo = ops.open_repository(nested)
    assert repo.path.resolve() == tmp_path.resolve()


def test_current_branch_reports_the_checked_out_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    assert repo.current_branch() == "main"


def test_branch_exists_true_and_false(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    assert repo.branch_exists("main") is True
    assert repo.branch_exists("does-not-exist") is False


def test_staging_precondition_passes_on_a_clean_index(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    repo.check_staging_precondition()  # must not raise


def test_staging_precondition_fails_closed_on_unexpected_staged_content(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "stray.txt").write_text("surprise\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "stray.txt"], check=True)

    repo = ops.open_repository(tmp_path)
    with pytest.raises(StagingPreconditionError) as excinfo:
        repo.check_staging_precondition()
    assert "stray.txt" in excinfo.value.staged_paths
    assert "UNEXPECTED STAGED CHANGES" in str(excinfo.value)

    # The runner never auto-recovers: the file is still staged afterward.
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "stray.txt" in result.stdout


def test_unstaged_working_tree_changes_do_not_violate_the_precondition(tmp_path: Path) -> None:
    """Only the REAL INDEX's relationship to HEAD matters (research.md §0.8) —
    uncommitted, unstaged edits are exactly what's expected to exist."""
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("edited but not staged\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("also not staged\n", encoding="utf-8")

    repo = ops.open_repository(tmp_path)
    repo.check_staging_precondition()  # must not raise


def test_tracked_files_under_reports_tracked_paths(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    assert repo.tracked_files_under("README.md") == ["README.md"]
    assert repo.tracked_files_under(".ai-runs") == []


def test_rev_parse_head_returns_a_40_char_oid(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    oid = repo.rev_parse("HEAD")
    assert len(oid) == 40
    assert all(c in "0123456789abcdef" for c in oid)


# --- Exit-code-semantics hardening (Codex finding: negative answers vs. real failures) ---


def test_branch_exists_raises_on_a_genuine_git_failure_not_a_false(tmp_path: Path) -> None:
    """A `rev-parse --verify` failure unrelated to ref existence (here:
    not a Git repository at all) must never be reported as `False`."""
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    # Build a GitRepo pointed at a non-repository path directly (bypassing
    # open_repository's own up-front check) to exercise branch_exists'
    # own error handling in isolation.
    repo = ops.GitRepo(path=plain_dir)
    with pytest.raises(GitAmbiguityError):
        repo.branch_exists("main")


def test_is_ancestor_true_for_a_commit_and_itself(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    head = repo.rev_parse("HEAD")
    assert repo.is_ancestor(head, head) is True


def test_is_ancestor_false_for_a_legitimate_non_ancestor(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    first = repo.rev_parse("HEAD")
    (tmp_path / "second.txt").write_text("more\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "second.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "second"], check=True)
    second = repo.rev_parse("HEAD")
    # `second` is a descendant of `first`, so `second` is NOT an ancestor of `first`.
    assert repo.is_ancestor(second, first) is False
    assert repo.is_ancestor(first, second) is True


def test_is_ancestor_raises_on_a_malformed_or_nonexistent_oid(tmp_path: Path) -> None:
    """Reproduces the Codex finding exactly: an invalid ancestry OID must
    raise, never silently answer `False`."""
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    head = repo.rev_parse("HEAD")
    with pytest.raises(GitAmbiguityError):
        repo.is_ancestor("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", head)


def test_tracked_files_under_raises_on_a_genuine_ls_files_failure(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    with pytest.raises(GitAmbiguityError):
        # An unsupported pathspec magic is a genuine `ls-files` error
        # (exit 128), never an empty/negative "no tracked files" result.
        repo.tracked_files_under(":(unsupportedmagic)foo")


def test_tracked_files_under_returns_empty_list_for_a_legitimate_no_match(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    assert repo.tracked_files_under("no-such-path") == []


def test_is_clean_staging_area_raises_on_a_genuine_git_failure(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    repo = ops.GitRepo(path=plain_dir)
    with pytest.raises(GitAmbiguityError):
        repo.is_clean_staging_area()


def test_is_path_ignored_true_when_covered_by_gitignore(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    repo = ops.open_repository(tmp_path)
    assert repo.is_path_ignored(".ai-runs") is True


def test_is_path_ignored_true_even_before_the_directory_exists_on_disk(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    assert not (tmp_path / ".ai-runs").exists()
    repo = ops.open_repository(tmp_path)
    assert repo.is_path_ignored(".ai-runs") is True


def test_is_path_ignored_false_when_not_covered_by_any_rule(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".ai-runs").mkdir()
    repo = ops.open_repository(tmp_path)
    assert repo.is_path_ignored(".ai-runs") is False


def test_is_path_ignored_false_for_an_untracked_but_unignored_directory_with_files(tmp_path: Path) -> None:
    """This is exactly the false-PASS scenario Codex reproduced against
    the old tracked-files-only check: an untracked, un-ignored directory
    with content in it must still be reported as "not ignored"."""
    _init_repo(tmp_path)
    ai_runs = tmp_path / ".ai-runs"
    ai_runs.mkdir()
    (ai_runs / "20260101-001-x-prompt.md").write_text("hi", encoding="utf-8")
    repo = ops.open_repository(tmp_path)
    assert repo.tracked_files_under(".ai-runs") == []  # not tracked...
    assert repo.is_path_ignored(".ai-runs") is False  # ...but also not ignored


def test_is_path_ignored_raises_on_a_genuine_git_failure(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    with pytest.raises(GitAmbiguityError):
        # A path outside the repository is a genuine check-ignore error
        # (exit 128), never a legitimate "not ignored" (exit 1) answer.
        repo.is_path_ignored("/definitely-outside-the-repo", is_directory=False)


def test_cat_file_tree_returns_an_oid_not_a_listing(tmp_path: Path) -> None:
    """Reproduces the Codex finding: the primitive must return the tree
    OBJECT ID, not `cat-file -p`'s pretty-printed tree listing."""
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    tree_oid = repo.cat_file_tree("HEAD")
    assert len(tree_oid) == 40
    assert all(c in "0123456789abcdef" for c in tree_oid)
    assert "README.md" not in tree_oid  # a listing would contain the filename


def test_cat_file_tree_matches_write_tree_for_the_same_content(tmp_path: Path) -> None:
    """The `Candidate Tree X -> staged tree X -> checkpoint commit tree X`
    invariant needs direct OID equality between `cat_file_tree(commit)`
    and `write_tree()` run against the same staged content."""
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    assert repo.cat_file_tree("HEAD") == repo.write_tree()


def test_cat_file_tree_raises_for_a_malformed_or_nonexistent_object(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    with pytest.raises(GitAmbiguityError):
        repo.cat_file_tree("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")


# --- git_dir / add_all (T027 Candidate Tree support primitives) --------


def test_git_dir_resolves_to_the_dot_git_directory_for_a_plain_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    assert repo.git_dir() == (tmp_path / ".git").resolve()


def test_git_dir_resolves_correctly_for_a_separate_git_dir_worktree_style_repo(tmp_path: Path) -> None:
    worktree = _make_separate_git_dir_worktree(tmp_path)
    external_gitdir = tmp_path / "external-gitdir"
    repo = ops.open_repository(worktree)
    assert repo.git_dir() == external_gitdir.resolve()


def test_add_all_stages_the_working_directory_into_the_targeted_index(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = ops.open_repository(tmp_path)
    (tmp_path / "new_file.txt").write_text("new\n", encoding="utf-8")
    repo.add_all()
    assert "new_file.txt" in [name for _, name in repo.staged_name_status()]
