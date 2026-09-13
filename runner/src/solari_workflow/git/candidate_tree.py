"""Isolated temporary-index Candidate Tree construction (research.md §12).

**Decision** (research.md §0.9/§12): Codex reviews the exact, uncommitted
state a block is proposing — the implementation must remain uncheckpointed
until QA approval. The Candidate Tree is how the runner turns that
on-disk, uncommitted state into a single, content-addressed Git object
(a tree OID) that Codex reviews and that a later `checkpoint` (git/
checkpoint.py, T040 — a later phase, not implemented here) can stage into
the real index and commit, without ever touching `.git/index` in the
meantime.

Construction (`GIT_INDEX_FILE`-redirected, never the real index):

1. seed a brand-new temporary index from `HEAD`'s tree (`git read-tree
   HEAD`);
2. stage the CURRENT WORKING DIRECTORY's on-disk content into that
   temporary index (`git add -A` — `.gitignore`-aware by construction, so
   `.ai-runs/` is automatically excluded, per research.md §12's own
   callout that this is exactly why the readiness gate's "`.ai-runs/`
   excluded from Git" check, T018/T019, is load-bearing);
3. `git write-tree` against that temporary index for the resulting tree
   OID;
4. always remove the temporary index file afterward, in a `finally`
   block, regardless of success or failure above.

Tracked modifications, tracked deletions, new non-ignored files, file
modes, and symlinks are all captured by `git add -A` in one pass — no
custom mode/symlink handling is needed here; Git's own `lstat`-based
staging already does the right thing (research.md §12).

**Deliberately no precondition on the working directory itself, and none
on the real staging area either** — this module builds the Candidate Tree
from the working directory alone and never reads or writes the real
index, so it has nothing to precondition against. The real staging-area
precondition (research.md §0.8, `GitRepo.check_staging_precondition`,
already implemented in `git/ops.py`, T010) is a *separate* concern that
belongs to the future call sites that DO write to the real index —
`start-block`, `run-codex-gate`, and `checkpoint` (T037/T039/T040, a later
phase of this feature, not implemented here) — and is deliberately not
invoked from inside this module. Building a Candidate Tree is always safe
to attempt regardless of the real index's state.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from solari_workflow.git.ops import GitRepo


@dataclass(frozen=True)
class CandidateTree:
    """The result of one Candidate Tree build (data-model.md's Fingerprint
    record borrows both fields directly — see `fingerprint/engine.py`)."""

    tree_oid: str
    head_oid: str


def build_candidate_tree(repo: GitRepo) -> CandidateTree:
    """Build a Candidate Tree of `repo`'s current working directory.

    Never reads or writes the real `.git/index` — every Git invocation
    this function makes is redirected, via `GIT_INDEX_FILE`, at a
    temporary index file living under `<repo>/.git/solari-workflow/`
    (outside the working tree itself, so it can never be picked up by a
    later `git add -A` against the real index, and outside `.ai-runs/` so
    it needs no readiness-gate exemption of its own). The temporary index
    file is always removed in a `finally` block, whether or not the build
    succeeds.

    `head_oid` is captured once, up front, via a single `git rev-parse
    HEAD`, and reused as the returned `CandidateTree.head_oid` — the same
    value the caller should bind into a Fingerprint (`fingerprint/
    engine.py`) alongside `tree_oid`, so the two are always observed
    together from one build, never re-queried separately at a slightly
    different moment.
    """
    head_oid = repo.rev_parse("HEAD")
    tmp_index_dir = repo.git_dir() / "solari-workflow"
    tmp_index_dir.mkdir(parents=True, exist_ok=True)
    tmp_index_path = tmp_index_dir / f"tmp-index-{uuid.uuid4().hex}"
    env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index_path)}
    try:
        repo.read_tree(head_oid, env=env)
        repo.add_all(env=env)
        tree_oid = repo.write_tree(env=env)
    finally:
        tmp_index_path.unlink(missing_ok=True)
    return CandidateTree(tree_oid=tree_oid, head_oid=head_oid)
