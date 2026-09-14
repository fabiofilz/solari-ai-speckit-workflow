"""Thin `git` subprocess wrappers over `platform/proc.py` (research.md §0.8, §12, §13).

Every later Git-touching operation (Candidate Tree construction,
checkpoint, retention) is expected to compose the primitives here rather
than shelling out to `git` independently — this keeps Git interaction
centralized in one module, per plan.md's Project Structure. No `shell=True`
anywhere; every invocation goes through `platform/proc.py`'s list-argv
`run`.

**Exit-code semantics for query primitives** (`branch_exists`,
`is_clean_staging_area`, `is_ancestor`, `is_path_ignored`,
`tracked_files_under`): each of these wraps a `git` subcommand whose exit
code has a *specific*, documented meaning beyond plain zero/nonzero —
e.g. `git merge-base --is-ancestor`: `0` = yes, `1` = no, anything else =
a genuine Git failure (bad object, corrupt repo, etc.), never a bare "no".
Every method below checks for the *exact* code(s) that mean a legitimate
negative/empty result and raises :class:`~solari_workflow.errors.GitAmbiguityError`
(never silently returns a falsy default) for anything else, so a Git
failure can never masquerade as an ordinary negative answer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from solari_workflow.errors import GitAmbiguityError, StagingPreconditionError
from solari_workflow.platform import proc

GIT_EXECUTABLE = "git"


def _git_metadata_exists_anywhere(path: str | os.PathLike[str]) -> bool:
    """Filesystem fact, independent of Git's own output or locale: does a
    `.git` entry (directory, or a worktree/submodule `.git` *file*) exist
    at `path` or any of its ancestor directories, up to the filesystem
    root?

    This is the deterministic, language-independent discriminator used
    by `is_git_repository`/`open_repository` to tell "no repository
    metadata anywhere" (an ordinary, ever-legitimate `False`/absence)
    apart from "repository metadata exists somewhere but Git could not
    resolve it" (corruption/ambiguity — must never be reported as mere
    absence). Git's own stderr text is **not** used for this decision:
    it can be locale-translated, and — as reproduced by Codex — the
    exact same English phrase (`fatal: not a git repository...`) is what
    Git emits both for genuine absence AND for a corrupted `.git`
    file/directory, so it cannot serve as the semantic source of truth
    here even in English.

    Any filesystem error while probing (permission denied, a race, an
    unresolvable path) fails toward `True` ("metadata might exist") —
    never toward a silent `False` a probe failure could not actually
    support; the caller escalates that to `GitAmbiguityError` rather than
    guessing "definitely absent".
    """
    try:
        current = Path(path).resolve()
    except OSError:
        return True
    while True:
        candidate = current / ".git"
        try:
            if candidate.is_symlink() or candidate.exists():
                return True
        except OSError:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _run_repository_probe(
    argv: list[str], path: str | os.PathLike[str]
) -> proc.ProcessResult:
    """Shared subprocess call for both repository-detection entry points.

    Converts a missing/inaccessible working directory (Python's
    `subprocess` raises a raw `FileNotFoundError`/`NotADirectoryError`
    for a nonexistent/non-directory `cwd`, before Git itself ever runs)
    into a structured :class:`~solari_workflow.errors.GitAmbiguityError`
    naming the requested path — callers must never see a raw stdlib
    `OSError` escape a repository-detection entry point.
    """
    executable = proc.resolve_executable(GIT_EXECUTABLE)
    try:
        return proc.run([executable, *argv], cwd=path, check=False)
    except OSError as exc:
        raise GitAmbiguityError(
            f"could not inspect '{path}' for a Git repository: {exc}"
        ) from exc


def is_git_repository(path: str | os.PathLike[str]) -> bool:
    """Whether `path` is inside a Git working tree.

    Fails closed rather than converting every nonzero exit into `False`.
    Classification is filesystem-based, not derived from Git's own
    (locale-sensitive, and in the corrupted-`.git`-pointer case genuinely
    ambiguous) stderr text — see `_git_metadata_exists_anywhere`:

    - Git succeeds -> its own `true`/`false` answer, verbatim.
    - Git fails AND no `.git` metadata exists anywhere at or above
      `path` -> ordinary, legitimate absence -> `False`.
    - Git fails BUT `.git` metadata exists somewhere (a malformed/empty
      `.git` directory, a broken worktree/submodule `.git` file pointer,
      or any other repository-adjacent state Git could not resolve) ->
      corruption/ambiguity, never absence -> `GitAmbiguityError`.
    - Any other unexpected Git failure with no local `.git` marker at all
      (a genuine I/O/process failure) also raises, rather than guessing.
    """
    result = _run_repository_probe(["rev-parse", "--is-inside-work-tree"], path)
    if result.ok:
        return result.stdout.strip() == "true"
    if not _git_metadata_exists_anywhere(path):
        return False
    raise GitAmbiguityError(
        f"'{path}' has Git repository metadata Git could not resolve "
        f"(exit {result.returncode}): {result.stderr.strip()}"
    )


def open_repository(path: str | os.PathLike[str]) -> "GitRepo":
    """Locate the Git repository containing `path`; fail closed if none exists.

    Implements the "expected repository not present → STOP" invariant
    (research.md §0.1's Runner-enforced invariants table). Uses the same
    filesystem-based, locale-independent classification as
    `is_git_repository` to distinguish genuine absence from corruption/
    ambiguity, rather than collapsing both into one generic "not
    present" diagnosis or relying on Git's own (potentially translated,
    and in the corrupted-pointer case genuinely overlapping) stderr text:

    - Git succeeds -> the resolved repository root.
    - Git fails AND no `.git` metadata exists anywhere at or above
      `path` -> genuine absence -> `GitAmbiguityError` naming "repository
      not present".
    - Git fails BUT `.git` metadata exists somewhere Git could not
      resolve -> corruption/ambiguity -> a distinctly worded
      `GitAmbiguityError`, never reported as mere absence.

    Git's own stderr is preserved in both raised messages for
    diagnostics, but is never the classification mechanism itself.
    """
    result = _run_repository_probe(["rev-parse", "--show-toplevel"], path)
    if result.ok:
        return GitRepo(path=Path(result.stdout.strip()))
    if not _git_metadata_exists_anywhere(path):
        raise GitAmbiguityError(
            f"no Git repository found at or above '{path}' (expected repository not "
            f"present): {result.stderr.strip()}"
        )
    raise GitAmbiguityError(
        f"'{path}' has Git repository metadata Git could not resolve into a usable "
        f"repository (exit {result.returncode}): {result.stderr.strip()}"
    )


@dataclass(frozen=True)
class GitRepo:
    """A thin, stateless handle onto a working directory's Git repository.

    Every method issues exactly one `git` invocation via
    `platform/proc.py` — no method mutates Python-level state, so a
    `GitRepo` is safe to construct repeatedly and pass around freely.
    """

    path: Path

    def _run(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> proc.ProcessResult:
        """Run one `git` subcommand, with a single, narrowly-scoped retry
        (research.md §0.6/A6): only an `OSError` raised by the subprocess
        layer itself — the `git` process failing to even launch (e.g. a
        transient fork/exec resource failure) — is retried, exactly once,
        via `proc.run_with_single_retry`. By construction, a process that
        never launched cannot have mutated anything, so this blind retry
        is always safe regardless of which git subcommand it was; no
        per-call-site "was it partially applied?" check is needed here.

        A `git` process that DID launch and merely exited nonzero is
        `CommandFailed`/plain nonzero data (`check=True`/`check=False`
        respectively) — never an `OSError` — and is therefore NEVER
        retried by this path: a merge conflict, a compare-and-swap
        `update-ref` rejection, an ambiguous ref, or any other
        structural/semantic Git failure stops immediately, exactly as
        A6 requires ("structural/semantic/ambiguous Git failures stop
        immediately... never introduce generic 'retry any Git failure'
        behavior").

        **Item 9 - this IS the complete safe set, not a placeholder.**
        research.md §0.6's own retry table names exactly one pre-mutation
        transient class: "claude/codex/git/uv failed to launch". No
        OTHER post-launch Git failure can be classified as transient
        without inspecting `stderr` TEXT for a specific cause (e.g. a
        `.git/index.lock` contention message) — and per this remediation's
        own explicit instruction, an unreliable, locale-sensitive stderr
        heuristic is worse than not retrying at all (this codebase
        already has one prior, hard-learned lesson about classifying Git
        state from translatable stderr text — see `is_git_repository`'s
        own docstring above). A nonzero exit is therefore always either
        a legitimate negative answer (handled by the specific caller,
        e.g. `branch_exists`'s exit-code table) or a structural failure
        (`CommandFailed`) — never something this method itself reclassifies
        as transient. This scope was deliberately re-examined for this
        remediation and is unchanged: it is the complete answer, not an
        interim one.
        """
        executable = proc.resolve_executable(GIT_EXECUTABLE)
        full_argv = [executable, *argv]

        def _invoke() -> proc.ProcessResult:
            try:
                return proc.run(full_argv, cwd=self.path, env=env, check=check, input_text=input_text)
            except OSError as exc:
                raise proc.TransientOperationalError(
                    f"failed to launch 'git {' '.join(argv)}': {exc}"
                ) from exc

        return proc.run_with_single_retry(_invoke)

    # --- Repository/state inspection -----------------------------------

    def rev_parse(self, rev: str = "HEAD") -> str:
        return self._run(["rev-parse", rev]).stdout.strip()

    def current_branch(self) -> str:
        """The branch HEAD is attached to, read from the symbolic ref itself
        (`git symbolic-ref --quiet HEAD` -> `refs/heads/<name>`), never via
        `rev-parse --abbrev-ref`, whose output is disambiguated against
        same-named tags (fourth remediation, B2). Detached HEAD -> "HEAD"."""
        result = self._run(["symbolic-ref", "--quiet", "HEAD"], check=False)
        if result.returncode == 1:
            return "HEAD"
        if result.returncode != 0:
            raise GitAmbiguityError(
                f"'git symbolic-ref HEAD' failed unexpectedly (exit {result.returncode}): {result.stderr.strip()}"
            )
        ref = result.stdout.strip()
        if not ref.startswith("refs/heads/"):
            raise GitAmbiguityError(f"HEAD points at non-branch ref {ref!r}")
        return ref[len("refs/heads/") :]

    def branch_oid(self, name: str) -> str:
        """The commit OID of the BRANCH `name`, resolved only through the
        explicit `refs/heads/<name>` ref - a tag or any other ref with the
        same short name can never be selected (fourth remediation, B2).
        A missing branch raises."""
        result = self._run(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", f"refs/heads/{name}^{{commit}}"], check=False
        )
        oid = result.stdout.strip()
        if result.returncode != 0 or not oid:
            raise GitAmbiguityError(f"branch ref 'refs/heads/{name}' does not resolve to a commit")
        return oid

    def branch_exists(self, name: str) -> bool:
        """`git rev-parse --verify --quiet refs/heads/<name>`.

        Exit `0` = the branch exists; `1` = it does not (per
        `--quiet`'s documented contract, this also covers a
        syntactically malformed ref name — `--verify --quiet` is
        Git's own recommended "does this ref exist" idiom and
        intentionally does not distinguish "malformed" from "absent").
        Any other exit code is a genuine Git failure, not a legitimate
        "no", and is raised rather than reported as `False`.
        """
        result = self._run(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], check=False)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitAmbiguityError(
            f"'git rev-parse --verify' failed unexpectedly (exit {result.returncode}) "
            f"while checking for branch {name!r}: {result.stderr.strip()}"
        )

    def is_clean_staging_area(self) -> bool:
        """`git diff --cached --quiet` — exit `0` means the real index matches HEAD.

        Exit `1` means it does not (a legitimate "dirty" answer); any
        other exit code (e.g. `128` for a corrupt repo or ambiguous
        argument) is a genuine Git failure and is raised, never reported
        as "clean" or "dirty".
        """
        result = self._run(["diff", "--cached", "--quiet"], check=False)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitAmbiguityError(
            f"'git diff --cached --quiet' failed unexpectedly (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    def staged_name_status(self) -> list[tuple[str, str]]:
        result = self._run(["diff", "--cached", "--name-status"])
        entries: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            status, _, name = line.partition("\t")
            entries.append((status, name))
        return entries

    def check_staging_precondition(self) -> None:
        """Fail-closed staged-state inspection (research.md §0.8).

        Reused at every call site that assumes a known-clean real index
        (`start-block`, `run-codex-gate`, `checkpoint`): raises
        :class:`StagingPreconditionError` naming the unexpectedly staged
        paths if the real index has drifted from `HEAD`. Never touches
        the index itself — no automatic `git reset`/`git stash` recovery.
        """
        if self.is_clean_staging_area():
            return
        staged_paths = [name for _, name in self.staged_name_status()]
        raise StagingPreconditionError(
            "WORKFLOW BLOCKED — UNEXPECTED STAGED CHANGES: " + ", ".join(staged_paths),
            staged_paths=staged_paths,
        )

    def diff_between(self, rev1: str, rev2: str) -> str:
        """`git diff <rev1> <rev2>` — a plain-text diff between two
        already-resolved Git revisions (commit-ish or tree-ish alike).

        A2 remediation: Codex's review evidence is built by diffing
        `HEAD` directly against Candidate Tree X's own `candidate_tree_oid`
        (a real, content-addressed tree OBJECT — `git write-tree`'s
        result), never against the live, mutable working directory. This
        provably binds the evidence to the exact state a `run-codex-gate`
        invocation already fingerprinted, and — unlike a working-tree
        diff — covers everything the invariant chain requires:

        - tracked modifications and deletions (an ordinary tree-to-tree
          diff);
        - new files, WITH their full content (a file present only in
          `rev2`'s tree renders as a complete "new file" diff — unlike a
          working-tree `git diff`, which never shows an untracked file at
          all, a tree-to-tree diff has no "untracked" concept: every path
          in either tree is a first-class, already-`add -A`'d entry);
        - file mode changes (part of git's own tree-diff machinery);
        - symlinks (a symlink is itself a blob — its target string — with
          mode `120000`; a target change renders as an ordinary content
          diff);
        - binary files: reported explicitly as `Binary files a/... and
          b/... differ` (git's own default, non-`--binary` behavior) —
          an explicit acknowledgment that content was omitted, never a
          silent gap.

        Read-only; never touches the real index (diffing two revisions
        needs no index write at all).
        """
        return self._run(["diff", rev1, rev2]).stdout

    def diff_numstat(self, rev1: str, rev2: str) -> list[tuple[str | None, str | None, str]]:
        """`git diff --numstat <rev1> <rev2>` — `(added, deleted, path)`
        per changed path; `added`/`deleted` are `None` for a path git
        itself reports as binary (numstat prints a literal `-\t-\t<path>`
        for those — locale-independent, unlike parsing the human-readable
        "Binary files ... differ" line, research.md's own prior lesson
        about not classifying on translatable stderr/stdout text).

        Used by item 5's binary-evidence construction to find exactly
        which paths in :meth:`diff_between`'s plain-text output are
        binary, so the caller can supplement them with deterministic
        identity metadata (:meth:`ls_tree_entry`/:meth:`blob_size`)
        instead of leaving a bare "Binary files differ" line as the only
        evidence.
        """
        # Third remediation (M7): machine-safe form only. `--no-renames`
        # disables rename detection (whatever the user's `diff.renames`
        # config says), so a rename is always reported as a deletion of
        # the old path plus an addition of the new one - never the
        # human-form `old => new` / `{a => b}` display string, which is
        # not a literal path. `-z` NUL-terminates each record so no path
        # is ever quoted/escaped. `--no-textconv` keeps binary
        # classification independent of any configured textconv driver.
        result = self._run(["diff", "--numstat", "-z", "--no-renames", "--no-textconv", rev1, rev2])
        entries: list[tuple[str | None, str | None, str]] = []
        for record in result.stdout.split("\0"):
            if not record:
                continue
            parts = record.split("\t", 2)
            if len(parts) != 3:
                raise GitAmbiguityError(f"unexpected 'git diff --numstat -z' record: {record!r}")
            added_raw, deleted_raw, path = parts
            added = None if added_raw == "-" else added_raw
            deleted = None if deleted_raw == "-" else deleted_raw
            entries.append((added, deleted, path))
        return entries

    def ls_tree_entries(self, tree_ish: str) -> dict[str, tuple[str, str, str]]:
        """Every entry of `tree_ish`, recursively, as `{path: (mode, type,
        oid)}` (`git ls-tree -r -z --full-tree`). NUL-delimited and
        pathspec-free, so a path is always matched literally - never
        quoted, and never interpreted as a glob pattern."""
        result = self._run(["ls-tree", "-r", "-z", "--full-tree", tree_ish], check=False)
        if not result.ok:
            raise GitAmbiguityError(
                f"'git ls-tree' failed unexpectedly (exit {result.returncode}) "
                f"for {tree_ish!r}: {result.stderr.strip()}"
            )
        entries: dict[str, tuple[str, str, str]] = {}
        for record in result.stdout.split("\0"):
            if not record:
                continue
            meta, sep, path = record.partition("\t")
            fields = meta.split()
            if not sep or len(fields) != 3:
                raise GitAmbiguityError(f"unexpected 'git ls-tree -z' record: {record!r}")
            mode, obj_type, oid = fields
            entries[path] = (mode, obj_type, oid)
        return entries

    def ls_tree_entry(self, tree_ish: str, path: str) -> tuple[str, str, str] | None:
        """`(mode, type, oid)` for the literal `path` within `tree_ish`, or
        `None` if `path` is absent from that tree."""
        return self.ls_tree_entries(tree_ish).get(path)

    def blob_size(self, oid: str) -> int:
        """`git cat-file -s <oid>` — a blob's size in bytes, without ever
        reading its content (safe for a binary file of unknown/large
        size, per item 5's "do not dump arbitrary huge binary content")."""
        result = self._run(["cat-file", "-s", oid], check=False)
        if not result.ok:
            raise GitAmbiguityError(
                f"'git cat-file -s' failed unexpectedly (exit {result.returncode}) for {oid!r}: "
                f"{result.stderr.strip()}"
            )
        return int(result.stdout.strip())

    def untracked_files(self) -> list[str]:
        """Paths not tracked by Git and not excluded by `.gitignore`
        (`git ls-files --others --exclude-standard`) — a live working-
        directory query, distinct from :meth:`diff_between`'s tree-object
        comparison (kept as a general utility; `run-codex-gate`'s own
        review-evidence construction no longer needs it now that a
        tree-to-tree diff against Candidate Tree X already renders new
        files with full content — see :meth:`diff_between`).
        """
        result = self._run(["ls-files", "--others", "--exclude-standard"], check=False)
        if not result.ok:
            raise GitAmbiguityError(
                f"'git ls-files --others' failed unexpectedly (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return [line for line in result.stdout.splitlines() if line]

    def tracked_files_under(self, path: str) -> list[str]:
        """Tracked files under `path` (relative to the repo root), if any.

        `git ls-files` exits `0` whether or not it finds any matches — an
        empty result is a legitimate, ordinary answer. It exits nonzero
        (typically `128`) only for a genuine failure (e.g. an invalid
        pathspec, or a path outside the repository), which is raised
        rather than silently reported as "no tracked files".
        """
        result = self._run(["ls-files", "--", path], check=False)
        if not result.ok:
            raise GitAmbiguityError(
                f"'git ls-files' failed unexpectedly (exit {result.returncode}) "
                f"for {path!r}: {result.stderr.strip()}"
            )
        return [line for line in result.stdout.splitlines() if line]

    def is_path_ignored(self, path: str, *, is_directory: bool = True) -> bool:
        """Whether `path` is excluded by Git's ignore rules (`git check-ignore`).

        Per `git-check-ignore(1)`: exit `0` = ignored, `1` = not ignored
        — both are legitimate, distinguishable answers, never conflated
        (unlike a tracked-files check, which can never distinguish "not
        tracked" from "not ignored"). Anything else (`128` and other
        unexpected exits) is a genuine Git failure and is raised rather
        than treated as "not ignored".

        `path` need not exist on disk — `check-ignore` evaluates pattern
        matching, not filesystem state — but a directory-only
        (trailing-slash) `.gitignore` pattern only matches when Git can
        tell the argument itself denotes a directory, which it cannot
        infer for a path that does not yet exist. `is_directory=True`
        (the default, matching this module's sole caller: the
        `.ai-runs/` readiness check) works around that by passing the
        path with an explicit trailing slash, which Git honors as a
        directory hint regardless of whether the path currently exists.
        """
        candidate = path.rstrip("/") + "/" if is_directory else path
        result = self._run(["check-ignore", "--quiet", "--", candidate], check=False)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitAmbiguityError(
            f"'git check-ignore' failed unexpectedly (exit {result.returncode}) "
            f"for {path!r}: {result.stderr.strip()}"
        )

    def tag_exists(self, name: str) -> bool:
        """`git rev-parse --verify --quiet refs/tags/<name>` — the tag-ref
        counterpart to :meth:`branch_exists`, used as a checkpoint-tag
        collision preflight (B3/M2: check before mutating anything, not
        after)."""
        result = self._run(["rev-parse", "--verify", "--quiet", f"refs/tags/{name}"], check=False)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitAmbiguityError(
            f"'git rev-parse --verify' failed unexpectedly (exit {result.returncode}) "
            f"while checking for tag {name!r}: {result.stderr.strip()}"
        )

    def parents_of(self, commit_ish: str) -> list[str]:
        """The parent OIDs of `commit_ish`, in order (`git rev-list
        --parents -n 1 <commit_ish>` — first token is `commit_ish` itself
        resolved to a full OID, the rest are its parents in the order Git
        recorded them). Used by the checkpoint merge-topology verification
        (B3): parent count, parent identity, and parent ORDER are each
        independently significant for an ordinary `git merge --no-ff`
        (first parent is the branch that was checked out, i.e. `main`;
        second is the branch that was merged in).
        """
        result = self._run(["rev-list", "--parents", "-n", "1", commit_ish], check=False)
        if not result.ok:
            raise GitAmbiguityError(
                f"'git rev-list --parents' failed unexpectedly (exit {result.returncode}) "
                f"for {commit_ish!r}: {result.stderr.strip()}"
            )
        tokens = result.stdout.split()
        if not tokens:
            raise GitAmbiguityError(f"'git rev-list --parents' returned no output for {commit_ish!r}")
        return tokens[1:]

    def object_type(self, oid: str) -> str:
        """`git cat-file -t <oid>` — used to prove an annotated tag ref
        really is a tag OBJECT (not a lightweight ref pointing directly at
        a commit), per B3's "annotated tag target is the verified merge
        commit" requirement."""
        result = self._run(["cat-file", "-t", oid], check=False)
        if not result.ok:
            raise GitAmbiguityError(
                f"'git cat-file -t' failed unexpectedly (exit {result.returncode}) "
                f"for {oid!r}: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def merged_branches(self, target: str = "main") -> list[str]:
        result = self._run(["branch", "--merged", target])
        branches: list[str] = []
        for line in result.stdout.splitlines():
            name = line.strip().lstrip("*").strip()
            if name:
                branches.append(name)
        return branches

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """`git merge-base --is-ancestor <ancestor> <descendant>`.

        Per `git-merge-base(1)`: exit `0` = yes, `1` = no — both
        legitimate answers. Any other exit code (e.g. `128` for a
        malformed or nonexistent object) is a genuine Git failure and is
        raised; an invalid OID must never be reported as "not an
        ancestor".
        """
        result = self._run(["merge-base", "--is-ancestor", ancestor, descendant], check=False)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitAmbiguityError(
            f"'git merge-base --is-ancestor' failed unexpectedly (exit {result.returncode}) "
            f"for ancestor={ancestor!r} descendant={descendant!r}: {result.stderr.strip()}"
        )

    # --- Branch/ref mutation (used by later checkpoint/branch tasks) ---

    def create_branch(self, name: str, start_point: str = "HEAD") -> None:
        self._run(["branch", name, start_point])

    def switch(self, branch: str) -> None:
        self._run(["switch", branch])

    def merge_no_ff(self, branch: str, message: str | None = None) -> None:
        argv = ["merge", "--no-ff", branch]
        if message is not None:
            argv.extend(["-m", message])
        self._run(argv)

    def tag_annotated(self, tag_name: str, message: str, target: str = "HEAD") -> None:
        """`git tag -a <name> <target> -m <message>` - creates the tag
        object AND publishes `refs/tags/<name>` in one atomic step. NOT
        used by the checkpoint flow's own tag publication (B3/item 3
        uses :meth:`mktag` + :meth:`update_ref` instead, precisely so the
        object can be verified BEFORE anything makes it visible by name)
        - kept as a general-purpose primitive for contexts where that
        distinction does not matter.
        """
        self._run(["tag", "-a", tag_name, target, "-m", message])

    def delete_tag(self, tag_name: str) -> None:
        """`git tag -d <name>` — a general-purpose primitive. Never used
        to delete an already-published, permanent checkpoint tag
        (research.md §13/constitution: checkpoint tags are never deleted
        or rewritten once genuinely published)."""
        self._run(["tag", "-d", tag_name])

    def tagger_ident(self) -> str:
        """`git var GIT_COMMITTER_IDENT` — the exact `"Name <email>
        timestamp tz"` line format a tag object's own `tagger` field
        requires (identical shape to a commit's `committer` line), read
        from the same identity `git tag -a` itself would use. Used by
        :meth:`mktag` to hand-construct a valid tag object via plumbing.
        """
        return self._run(["var", "GIT_COMMITTER_IDENT"]).stdout.strip()

    def build_tag_payload(self, *, target: str, tag_name: str, message: str) -> str:
        """The exact annotated-tag object payload `mktag` will write
        (`object`/`type`/`tag`/`tagger` headers, blank line, message).
        Built once, so the caller can later compare the object actually
        stored under the returned OID against these exact bytes."""
        tagger = self.tagger_ident()
        return f"object {target}\ntype commit\ntag {tag_name}\ntagger {tagger}\n\n{message}"

    def mktag(self, payload: str) -> str:
        """`git mktag` — writes a validated annotated tag OBJECT from
        `payload` to the object database and returns its OID, WITHOUT
        creating or touching any ref. The returned object is unreferenced
        and invisible by name until a separate, later
        `update_ref(f"refs/tags/{name}", oid, "")` publishes it, so it
        can be fully verified first (B3). Contrast with `tag_annotated`,
        which creates the object AND the ref together."""
        return self._run(["mktag"], input_text=payload).stdout.strip()

    def read_tag_object(self, oid: str) -> str:
        """`git cat-file tag <oid>` — the raw stored payload of the tag
        object with exactly this OID (never resolved through a ref
        name), so it can be compared byte-for-byte with the payload the
        caller asked `mktag` to write."""
        result = self._run(["cat-file", "tag", oid], check=False)
        if not result.ok:
            raise GitAmbiguityError(
                f"'git cat-file tag' failed unexpectedly (exit {result.returncode}) for {oid!r}: "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    def update_ref(self, ref: str, new_oid: str, old_oid: str) -> None:
        """Compare-and-swap ref update — fails if `ref` isn't currently `old_oid`."""
        self._run(["update-ref", ref, new_oid, old_oid])

    def delete_branch(self, name: str, *, force: bool = False) -> None:
        flag = "-D" if force else "-d"
        self._run(["branch", flag, name])

    # --- Plumbing used by Candidate Tree / checkpoint construction ------

    def commit_tree(self, tree_oid: str, parent_oid: str, message: str) -> str:
        return self._run(["commit-tree", tree_oid, "-p", parent_oid, "-m", message]).stdout.strip()

    def write_tree(self, env: dict[str, str] | None = None) -> str:
        return self._run(["write-tree"], env=env).stdout.strip()

    def read_tree(self, tree_ish: str, env: dict[str, str] | None = None) -> None:
        self._run(["read-tree", tree_ish], env=env)

    def add_all(self, env: dict[str, str] | None = None) -> None:
        """`git add -A` — `.gitignore`-aware staging of the current working directory.

        Used by `git/candidate_tree.py` (research.md §12) against a
        `GIT_INDEX_FILE`-redirected temporary index; also usable directly
        against the real index by a caller that has already verified the
        real staging-area precondition (research.md §0.8) — this method
        itself performs no such check, since which index it targets is
        entirely determined by `env`.
        """
        self._run(["add", "-A"], env=env)

    def checkout_index_all(self) -> None:
        self._run(["checkout-index", "-a", "-f"])

    def git_dir(self) -> Path:
        """Absolute path to this repository's `.git` directory.

        Resolved via `git rev-parse --git-dir` rather than assuming
        `<repo>/.git` — correct for a plain repository, a `git worktree
        add` checkout, and a `--separate-git-dir` layout alike (the same
        distinction `open_repository`/`is_git_repository` already handle
        for repository *detection*). `git rev-parse --git-dir` prints a
        path relative to this call's `cwd` in the plain-repository case,
        so a relative result is resolved against `self.path` before
        returning.
        """
        raw = self._run(["rev-parse", "--git-dir"]).stdout.strip()
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.path / candidate
        return candidate.resolve()

    def cat_file_tree(self, commit_ish: str) -> str:
        """Resolve `commit_ish`'s **tree object OID** (not its listing).

        The invariant chain `Candidate Tree X -> staged tree X ->
        checkpoint commit tree X` (research.md §12/§13) requires a raw
        OID here, directly comparable by equality to a Candidate Tree's
        `git write-tree` result. `git rev-parse --verify <commit_ish>^{tree}`
        is the correct plumbing for that — it resolves and prints just
        the 40-character tree object id.

        NOTE: `tasks.md`/research.md's own illustrative command for this
        primitive is `cat-file -p <oid>^{tree}`, which instead prints the
        tree's *listing* (mode/type/oid/name lines for each entry) — not
        an object id at all, and useless for the equality check the
        invariant chain above depends on. This is a documentation defect
        in those planning artifacts, not a design choice to preserve;
        implemented here per the actually-required contract
        (`rev-parse --verify ...^{tree}`) and reported to the
        Orchestrator rather than silently "corrected" in the planning
        docs themselves.

        Fails closed (`GitAmbiguityError`) for a malformed or nonexistent
        object rather than returning anything misleading.
        """
        result = self._run(["rev-parse", "--verify", f"{commit_ish}^{{tree}}"], check=False)
        if not result.ok:
            raise GitAmbiguityError(
                f"could not resolve tree OID for {commit_ish!r} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
        oid = result.stdout.strip()
        if not oid or "\n" in oid or any(c not in "0123456789abcdefABCDEF" for c in oid):
            raise GitAmbiguityError(
                f"unexpected output resolving tree OID for {commit_ish!r}: {oid!r}"
            )
        return oid
