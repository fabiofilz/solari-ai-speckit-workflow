"""Runner-owned checkpoint flow (research.md §13, T040).

This module performs the ONE checkpoint commit itself, built directly
from the already-gated Candidate Tree via Git plumbing (`commit-tree`/
`update-ref`), never `git commit`. It is the sole code path that ever
runs `git merge --no-ff` or creates a checkpoint tag.

Branch retention (research.md §15, US5/T064-T066) is deliberately NOT
invoked from here; its call site belongs to that later phase.

**Eligibility (`preflight`, pure reads only).** Checkpoint requires ALL of:

1. no recorded checkpoint mutation boundary (a prior partial checkpoint
   failure is never continued automatically);
2. a lifecycle `state` that is explicitly checkpoint-eligible (`RUNNING`
   right after an eligible gate, or `READY_TO_RESUME` set by `resume`) —
   `BLOCKED_MANUAL` is never eligible, regardless of cached gate data;
3. the block branch is checked out and the real index is clean;
4. `last_safe_stage == GATE_PASSED` with a cached `last_gate`;
5. the IMMUTABLE structured gate-identity evidence referenced by state
   (`state/gate_identity.py`) exists, matches its recorded digest,
   records an eligible PASS, and every cached identity field (Fingerprint
   X, canonical feature identity, gated main) matches it exactly;
6. the authorized base still agrees everywhere:
   `last_gate.head_oid == expected_head_oid == gated_main_oid ==
   current main`;
7. a freshly rebuilt Candidate Tree/Fingerprint still equals Fingerprint X;
8. the checkpoint tag name does not exist.

**Durable pre-mutation protection (fourth remediation, B4).** After the
pure preflight and all message rendering/validation, and BEFORE the first
real mutation, the block-state is atomically written (fsync'd) as
`BLOCKED_MANUAL` with `checkpoint_mutation_boundary =
"mutation_intent_recorded"` and read back; if that write or read-back
fails, no mutation starts. From then on, a crash, kill, exception or a
later failed state write leaves that conservative marker in force: every
lifecycle command (`run-codex-gate`, `run-claude`, `resume`, `checkpoint`,
`start-block`) refuses until manual recovery. Final completion publication
copies the marker into a durable guard before replacing the state file;
only a receipt made visible after the final directory sync makes
`COMPLETED` authoritative. Nothing clears the marker based on how the
repository currently looks.

**Mutation envelope.** The first real workflow mutation is staging the
Candidate Tree into the real index. Everything from that call through
tag publication runs inside one envelope; every exception there becomes
:class:`CheckpointPartialFailureError` carrying the most truthful known
mutation boundary (`stage`) plus best-effort read-only observations of
the branch/main/tag refs, and the caller records `BLOCKED_MANUAL`.

**VERIFIED is visible only at the very end.** The checkpoint (block
branch) commit message identifies the block, task range and Candidate
Tree only — it never claims verification. The annotated tag is the sole
verified artifact: its exact payload is built, written as an
UNREFERENCED object via `git mktag`, re-read by its exact OID and
compared byte-for-byte with the expected payload, its peeled target and
the merge topology are re-checked, and only then is `refs/tags/<name>`
published with a compare-and-swap that requires the name not to exist.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from solari_workflow.config.schema import WorkflowConfig
from solari_workflow.errors import GitAmbiguityError, WorkflowError
from solari_workflow.fingerprint.engine import (
    Fingerprint,
    compute_fingerprint,
    fingerprint_to_dict,
    verify_fingerprint,
)
from solari_workflow.git.branch import (
    build_checkpoint_tag_name,
    parse_branch_name,
    validate_block_name,
    validate_task_id,
)
from solari_workflow.git.candidate_tree import build_candidate_tree
from solari_workflow.git.ops import GitRepo
from solari_workflow.speckit.integration import lookup_task_titles
from solari_workflow.state import block_state as block_state_store
from solari_workflow.state.block_state import BlockState
from solari_workflow.state.gate_identity import GateIdentityError, load_eligible_gate_identity

CHECKPOINT_FINGERPRINT_MISMATCH_MESSAGE = "CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE"
CHECKPOINT_MAIN_ADVANCED_MESSAGE = "CHECKPOINT BLOCKED — MAIN ADVANCED SINCE GATE"
CHECKPOINT_BASE_MISMATCH_MESSAGE = "CHECKPOINT BLOCKED — GATED BASE IDENTITY DOES NOT AGREE"
CHECKPOINT_GATE_EVIDENCE_MESSAGE = "CHECKPOINT BLOCKED — GATE IDENTITY NOT PROVEN BY IMMUTABLE EVIDENCE"

_CHECKPOINT_ELIGIBLE_STATES = ("RUNNING", "READY_TO_RESUME")

# Ordered mutation boundaries (M4/M5). Each value names the last point
# the flow is KNOWN to have reached when a failure occurred; see
# `_STAGE_FACTS` for exactly what each one does and does not prove.
STAGE_MUTATION_INTENT_RECORDED = "mutation_intent_recorded"
STAGE_INDEX_STAGING_STARTED = "index_staging_started"
STAGE_INDEX_STAGED = "index_staged_verified"
STAGE_COMMIT_OBJECT_CREATE_ATTEMPTED = "commit_object_create_attempted"
STAGE_COMMIT_OBJECT_CREATED = "commit_object_created"
STAGE_BRANCH_REF_UPDATE_ATTEMPTED = "branch_ref_update_attempted"
STAGE_BRANCH_REF_ADVANCED = "branch_ref_advanced"
STAGE_MAIN_SWITCH_ATTEMPTED = "main_switch_attempted"
STAGE_MERGE_ATTEMPTED = "merge_attempted"
STAGE_MERGE_VERIFIED = "merge_verified"
STAGE_TAG_OBJECT_CREATE_ATTEMPTED = "tag_object_create_attempted"
STAGE_TAG_OBJECT_CREATED = "tag_object_created"
STAGE_TAG_OBJECT_VERIFIED = "tag_object_verified"
STAGE_TAG_REF_PUBLISH_ATTEMPTED = "tag_ref_publish_attempted"
STAGE_TAG_REF_PUBLISHED = "tag_ref_published"

# Fourth remediation (M1): every statement distinguishes what is PROVEN
# (an operation was not invoked, or reported success and was re-checked)
# from what is NOT proven. A Git operation that reported failure may still
# have produced side effects (e.g. an unreachable object), so a failed
# operation is never described as "not created" / "not made".
_STAGE_FACTS: dict[str, str] = {
    STAGE_MUTATION_INTENT_RECORDED: (
        "checkpoint mutation protection was durably recorded before any Git mutation; this "
        "marker alone does NOT prove whether any later Git mutation started or completed "
        "(the process may have stopped at any point after recording it)"
    ),
    STAGE_INDEX_STAGING_STARTED: (
        "real-index staging was started and did not complete verification - the real index "
        "and working tree MAY be modified (unreferenced tree objects MAY exist); commit "
        "creation, ref updates, merge and tag operations were NOT attempted"
    ),
    STAGE_INDEX_STAGED: (
        "the real index was staged and verified to equal Candidate Tree X; checkpoint commit "
        "object creation (commit-tree) was NOT attempted; ref updates, merge and tag "
        "operations were NOT attempted"
    ),
    STAGE_COMMIT_OBJECT_CREATE_ATTEMPTED: (
        "the real index was staged and verified to equal Candidate Tree X; checkpoint commit "
        "object creation (commit-tree) was ATTEMPTED but NOT PROVEN - an unreferenced commit "
        "object MAY exist; ref updates, merge and tag operations were NOT attempted"
    ),
    STAGE_COMMIT_OBJECT_CREATED: (
        "an unreferenced checkpoint commit object was reported created; the block branch ref "
        "update, merge and tag operations were NOT attempted"
    ),
    STAGE_BRANCH_REF_UPDATE_ATTEMPTED: (
        "the block branch ref update was attempted but did not report success - whether the "
        "branch ref moved is NOT proven (see observations); merge and tag operations were NOT "
        "attempted"
    ),
    STAGE_BRANCH_REF_ADVANCED: (
        "the block branch ref update reported success; its follow-up verification or the "
        "switch to main did not start or did not complete - switching to main, merge and tag "
        "operations were NOT attempted"
    ),
    STAGE_MAIN_SWITCH_ATTEMPTED: (
        "switching the working tree to main was attempted and not verified - HEAD, the "
        "index and the working tree MAY be modified; merge and tag operations were NOT attempted"
    ),
    STAGE_MERGE_ATTEMPTED: (
        "the merge into main was attempted and did not complete verification - main's ref, "
        "the index, the working tree and merge metadata MAY be modified (state uncertain); "
        "tag object creation and tag ref publication were NOT attempted"
    ),
    STAGE_MERGE_VERIFIED: (
        "the merge into main was completed and verified; tag payload preparation did not "
        "complete - tag object creation (mktag) was NOT attempted; tag ref publication was NOT "
        "attempted"
    ),
    STAGE_TAG_OBJECT_CREATE_ATTEMPTED: (
        "the merge into main was completed and verified; tag object creation (mktag) was "
        "ATTEMPTED but NOT PROVEN - an unreferenced tag object MAY exist; tag ref publication "
        "was NOT attempted"
    ),
    STAGE_TAG_OBJECT_CREATED: (
        "mktag REPORTED a tag object (its OID is known), but its verification did NOT "
        "complete - its correctness is NOT proven; tag ref publication was NOT attempted"
    ),
    STAGE_TAG_OBJECT_VERIFIED: (
        "a tag object was written and verified; tag ref publication was NOT attempted"
    ),
    STAGE_TAG_REF_PUBLISH_ATTEMPTED: (
        "tag ref publication was attempted but did not report success - whether the tag ref "
        "exists is NOT proven (see observations)"
    ),
    STAGE_TAG_REF_PUBLISHED: (
        "the tag ref publication reported success; a later step failed - inspect the tag directly"
    ),
}


class CheckpointBlockedError(WorkflowError):
    """Checkpoint refused during `preflight` - by construction, nothing
    has been mutated. `exit_code = 1` (resolvable by re-gating)."""

    exit_code = 1


class CheckpointPartialFailureError(WorkflowError):
    """A failure after the first real mutation began. `stage` is the most
    truthful known mutation boundary. `exit_code = 2`; never auto-retried,
    never auto-repaired."""

    def __init__(self, message: str, *, stage: str, tag_object_oid: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.tag_object_oid = tag_object_oid


class CheckpointIntentNotRecordedError(CheckpointBlockedError):
    """The durable pre-mutation protection marker could not be written and
    proven (fourth remediation, B4). No Git mutation was started.
    `exit_code = 2` (a state-persistence failure needs manual attention)."""

    exit_code = 2


@dataclass(frozen=True)
class CheckpointOutcome:
    tag_name: str
    checkpoint_commit_oid: str
    merge_commit_sha: str


def _no_eligible_gate_message(block_state: BlockState) -> str:
    if block_state.last_gate is None:
        return (
            "no eligible PASS gate is recorded for this block; run "
            "'run-codex-gate' and obtain a checkpoint-eligible PASS before "
            "attempting a checkpoint"
        )
    return (
        "the recorded gate for this block is not checkpoint-eligible (state="
        f"{block_state.state!r}, last_safe_stage={block_state.last_safe_stage!r}); "
        "run 'run-codex-gate' again and obtain a PASS with no blocking-severity findings"
    )


def _verify_gate_evidence(block_state: BlockState, ai_runs_dir: Path) -> None:
    """Condition 5: the cached gate identity is proven by the immutable
    structured evidence, field for field."""
    try:
        identity = load_eligible_gate_identity(
            ai_runs_dir, block_state.gate_identity_record, block_state.gate_identity_sha256
        )
    except GateIdentityError as exc:
        raise CheckpointBlockedError(f"{CHECKPOINT_GATE_EVIDENCE_MESSAGE}: {exc}") from exc
    assert block_state.last_gate is not None
    mismatches = []
    if fingerprint_to_dict(identity.fingerprint) != fingerprint_to_dict(block_state.last_gate):
        mismatches.append("Fingerprint X")
    if identity.feature_identity != block_state.gate_feature_identity:
        mismatches.append("gate feature identity")
    if identity.feature_identity != block_state.tasks_md_path:
        mismatches.append("block feature identity")
    if identity.gated_main_oid != block_state.gated_main_oid:
        mismatches.append("gated main identity")
    if mismatches:
        raise CheckpointBlockedError(
            f"{CHECKPOINT_GATE_EVIDENCE_MESSAGE}: cached block-state does not match the immutable "
            f"gate evidence {block_state.gate_identity_record!r} ({', '.join(mismatches)}); re-run "
            "'run-codex-gate'"
        )


def preflight(
    repo: GitRepo, config: WorkflowConfig, block_state: BlockState, *, ai_runs_dir: Path
) -> Fingerprint:
    """Every eligibility condition (module docstring), in order, BEFORE any
    mutation. Returns the verified Fingerprint X."""
    if block_state.checkpoint_mutation_boundary is not None:
        raise GitAmbiguityError(
            "checkpoint refused: a previous checkpoint attempt failed after mutating Git state "
            f"(boundary {block_state.checkpoint_mutation_boundary!r}); manual recovery is required"
        )
    if block_state.state not in _CHECKPOINT_ELIGIBLE_STATES:
        raise CheckpointBlockedError(
            f"checkpoint refused: block-state is {block_state.state!r}, which is never "
            "checkpoint-eligible regardless of any cached gate data "
            f"(eligible states: {', '.join(_CHECKPOINT_ELIGIBLE_STATES)})"
        )

    # B3: persisted identity that reaches Git messages is re-validated
    # before anything else consumes it.
    _validate_message_identity(block_state, {})

    branch_name = block_state.branch_name
    current = repo.current_branch()
    if current != branch_name:
        raise GitAmbiguityError(
            f"checkpoint refused: current branch {current!r} does not match the "
            f"active block's branch {branch_name!r}"
        )
    if branch_name == config.project.main_branch:
        raise GitAmbiguityError(
            "checkpoint refused: current branch is the main branch "
            f"{config.project.main_branch!r}, not a development block branch"
        )

    repo.check_staging_precondition()

    if block_state.last_gate is None or block_state.last_safe_stage != "GATE_PASSED":
        raise CheckpointBlockedError(_no_eligible_gate_message(block_state))
    fingerprint_x = block_state.last_gate

    _verify_gate_evidence(block_state, ai_runs_dir)

    current_main_oid = repo.branch_oid(config.project.main_branch)
    if current_main_oid != block_state.gated_main_oid:
        raise CheckpointBlockedError(CHECKPOINT_MAIN_ADVANCED_MESSAGE)
    if not (
        block_state.expected_head_oid is not None
        and fingerprint_x.head_oid == block_state.expected_head_oid == block_state.gated_main_oid
    ):
        raise CheckpointBlockedError(
            f"{CHECKPOINT_BASE_MISMATCH_MESSAGE}: gated head={fingerprint_x.head_oid!r}, "
            f"expected head={block_state.expected_head_oid!r}, gated main={block_state.gated_main_oid!r}"
        )

    fresh_candidate = build_candidate_tree(repo)
    fresh_fingerprint = compute_fingerprint(
        repo, fresh_candidate, first_task=block_state.first_task, last_task=block_state.last_task
    )
    if not verify_fingerprint(fingerprint_x, fresh_fingerprint):
        raise CheckpointBlockedError(CHECKPOINT_FINGERPRINT_MISMATCH_MESSAGE)

    tag_name = build_checkpoint_tag_name(block_state.first_task, block_state.last_task)
    if repo.tag_exists(tag_name):
        raise CheckpointBlockedError(
            f"checkpoint refused: tag {tag_name!r} already exists; a checkpoint tag is a "
            "permanent historical reference and is never overwritten or recreated"
        )

    return fingerprint_x


def _require_single_line(label: str, value: str | None) -> None:
    """B3 defense in depth: a value interpolated into a line-oriented Git
    message must not contain a line break or any control/format/separator
    character that could start an additional message line."""
    if value is None:
        return
    for ch in value:
        if ch in "\r\n" or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            raise CheckpointBlockedError(
                f"checkpoint refused: {label} {value!r} contains a line break or control character "
                "and cannot be rendered into a Git message"
            )


def _validate_message_identity(block_state: BlockState, titles: dict[str, str | None]) -> None:
    """Pure, pre-mutation validation of every persisted identity value that
    reaches commit/merge/tag messages (fourth remediation, B3)."""
    try:
        validate_block_name(block_state.block_name)
        validate_task_id(block_state.first_task)
        validate_task_id(block_state.last_task)
    except WorkflowError as exc:
        raise CheckpointBlockedError(f"checkpoint refused: persisted block identity is invalid: {exc}") from exc
    if parse_branch_name(block_state.branch_name) is None:
        raise CheckpointBlockedError(
            f"checkpoint refused: persisted branch name {block_state.branch_name!r} is not a workflow branch name"
        )
    _require_single_line("feature identity", block_state.tasks_md_path)
    _require_single_line("gate evidence record", block_state.gate_identity_record)
    for task_id, title in titles.items():
        _require_single_line(f"task title for {task_id}", title)


def mutation_intent_state(block_state: BlockState) -> BlockState:
    """The exact record persisted before the first checkpoint mutation."""
    return replace(
        block_state,
        state="BLOCKED_MANUAL",
        checkpoint_mutation_boundary=STAGE_MUTATION_INTENT_RECORDED,
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _persist_mutation_intent(ai_runs_dir: Path, block_state: BlockState) -> None:
    marker = mutation_intent_state(block_state)
    try:
        block_state_store.write_block_state(ai_runs_dir, marker)
        persisted = block_state_store.load_block_state(ai_runs_dir)
    except Exception as exc:  # noqa: BLE001 - any failure means "not proven"
        raise CheckpointIntentNotRecordedError(
            "CHECKPOINT BLOCKED — the durable pre-mutation protection marker could not be recorded "
            f"({exc}); no Git mutation was started"
        ) from exc
    if persisted is None or block_state_store.to_dict(persisted) != block_state_store.to_dict(marker):
        raise CheckpointIntentNotRecordedError(
            "CHECKPOINT BLOCKED — the durable pre-mutation protection marker did not read back "
            "identically; no Git mutation was started"
        )


def _render_commit_message(block_state: BlockState, fingerprint_x: Fingerprint) -> str:
    """The checkpoint (block branch) commit message. Deliberately makes NO
    verification claim - this commit exists before merge and tag
    verification have run (B3A)."""
    return (
        f"Checkpoint commit: {block_state.block_name}\n"
        "\n"
        f"Branch: {block_state.branch_name}\n"
        f"Task range: {block_state.first_task}-{block_state.last_task}\n"
        f"Candidate tree: {fingerprint_x.candidate_tree_oid}\n"
        f"Base: {fingerprint_x.head_oid}\n"
    )


def _render_tag_message(
    block_state: BlockState, titles: dict[str, str | None], fingerprint_x: Fingerprint
) -> str:
    """Tag metadata (research.md §14) plus the gate identity fields. Only
    ever becomes visible through the final, verified tag ref."""
    lines = [
        f"Checkpoint: {block_state.block_name}",
        "",
        f"Branch: {block_state.branch_name}",
        f"Task range: {block_state.first_task}-{block_state.last_task}",
        "Tasks:",
    ]
    if titles:
        for task_id, title in titles.items():
            rendered = title if title is not None else "(title not found in tasks.md)"
            lines.append(f"  - {task_id}: {rendered}")
    else:
        lines.append("  (task titles unavailable - no resolved Spec Kit feature identity for this block)")
    lines.extend(
        [
            f"Candidate tree: {fingerprint_x.candidate_tree_oid}",
            f"Base: {fingerprint_x.head_oid}",
            f"Feature: {block_state.tasks_md_path if block_state.tasks_md_path is not None else '(none)'}",
            f"Gate evidence: {block_state.gate_identity_record} sha256:{block_state.gate_identity_sha256}",
            "Gate: PASS",
            "Checkpoint: VERIFIED",
        ]
    )
    return "\n".join(lines) + "\n"


def run_checkpoint(
    repo: GitRepo,
    config: WorkflowConfig,
    block_state: BlockState,
    tasks_md_path: Path | None,
    *,
    ai_runs_dir: Path,
) -> CheckpointOutcome:
    """Execute the checkpoint (module docstring). Returns only once the tag
    ref is published after full verification."""
    fingerprint_x = preflight(repo, config, block_state, ai_runs_dir=ai_runs_dir)
    tag_name = build_checkpoint_tag_name(block_state.first_task, block_state.last_task)
    main_branch = config.project.main_branch
    main_oid_before = block_state.gated_main_oid
    assert main_oid_before is not None

    # Pure reads/rendering, still outside the mutation envelope.
    titles = (
        lookup_task_titles(tasks_md_path, block_state.first_task, block_state.last_task)
        if tasks_md_path is not None
        else {}
    )
    _validate_message_identity(block_state, titles)
    commit_message = _render_commit_message(block_state, fingerprint_x)
    merge_message = f"Merge {block_state.branch_name} ({block_state.block_name})"
    for label, message in (("checkpoint commit message", commit_message), ("merge commit message", merge_message)):
        if "checkpoint: verified" in message.lower():
            raise CheckpointBlockedError(f"checkpoint refused: the {label} would contain a VERIFIED claim")
    tag_message = _render_tag_message(block_state, titles, fingerprint_x)

    # B4: durable pre-mutation protection. Until this marker is written AND
    # read back identically, no Git mutation may start; once it is on disk,
    # any abnormal termination leaves the block BLOCKED_MANUAL with a
    # checkpoint mutation boundary, which every lifecycle command refuses.
    _persist_mutation_intent(ai_runs_dir, block_state)

    stage = STAGE_INDEX_STAGING_STARTED
    new_commit_oid: str | None = None
    tag_object_oid: str | None = None
    try:
        # First real mutation: the real index / working tree.
        repo.read_tree(fingerprint_x.candidate_tree_oid)
        repo.checkout_index_all()
        staged_tree_oid = repo.write_tree()
        if staged_tree_oid != fingerprint_x.candidate_tree_oid:
            raise WorkflowError(
                f"staged real-index tree {staged_tree_oid!r} does not match the gated candidate tree "
                f"{fingerprint_x.candidate_tree_oid!r}"
            )
        stage = STAGE_INDEX_STAGED

        stage = STAGE_COMMIT_OBJECT_CREATE_ATTEMPTED
        new_commit_oid = repo.commit_tree(fingerprint_x.candidate_tree_oid, fingerprint_x.head_oid, commit_message)
        stage = STAGE_COMMIT_OBJECT_CREATED

        stage = STAGE_BRANCH_REF_UPDATE_ATTEMPTED
        repo.update_ref(f"refs/heads/{block_state.branch_name}", new_commit_oid, fingerprint_x.head_oid)
        stage = STAGE_BRANCH_REF_ADVANCED

        commit_tree_oid = repo.cat_file_tree(new_commit_oid)
        if commit_tree_oid != fingerprint_x.candidate_tree_oid:
            raise WorkflowError(
                f"checkpoint commit tree {commit_tree_oid!r} does not match the gated candidate tree "
                f"{fingerprint_x.candidate_tree_oid!r}"
            )

        stage = STAGE_MAIN_SWITCH_ATTEMPTED
        repo.switch(main_branch)
        if repo.current_branch() != main_branch or repo.rev_parse("HEAD") != main_oid_before:
            raise WorkflowError(
                f"after switching, HEAD is not branch refs/heads/{main_branch} at the gated main {main_oid_before!r}"
            )

        stage = STAGE_MERGE_ATTEMPTED
        repo.merge_no_ff(
            f"refs/heads/{block_state.branch_name}", message=merge_message
        )
        merge_commit_sha = repo.rev_parse("HEAD")
        parents = repo.parents_of(merge_commit_sha)
        if parents != [main_oid_before, new_commit_oid]:
            raise WorkflowError(
                f"merge commit parents {parents!r} are not exactly [{main_oid_before!r}, {new_commit_oid!r}]"
            )
        merge_tree_oid = repo.cat_file_tree(merge_commit_sha)
        if merge_tree_oid != fingerprint_x.candidate_tree_oid:
            raise WorkflowError(
                f"merge commit tree {merge_tree_oid!r} does not equal the gated candidate tree "
                f"{fingerprint_x.candidate_tree_oid!r}"
            )
        if repo.branch_oid(main_branch) != merge_commit_sha:
            raise WorkflowError(f"{main_branch!r} does not point at the verified merge commit {merge_commit_sha!r}")
        stage = STAGE_MERGE_VERIFIED

        expected_payload = repo.build_tag_payload(target=merge_commit_sha, tag_name=tag_name, message=tag_message)
        stage = STAGE_TAG_OBJECT_CREATE_ATTEMPTED
        tag_object_oid = repo.mktag(expected_payload)
        stage = STAGE_TAG_OBJECT_CREATED

        _verify_tag_object(
            repo,
            tag_object_oid=tag_object_oid,
            expected_payload=expected_payload,
            tag_name=tag_name,
            merge_commit_sha=merge_commit_sha,
            tag_message=tag_message,
            main_branch=main_branch,
        )
        stage = STAGE_TAG_OBJECT_VERIFIED

        stage = STAGE_TAG_REF_PUBLISH_ATTEMPTED
        repo.update_ref(f"refs/tags/{tag_name}", tag_object_oid, "")
        stage = STAGE_TAG_REF_PUBLISHED
    except Exception as exc:  # noqa: BLE001 - re-classified, never swallowed
        raise CheckpointPartialFailureError(
            _partial_failure_message(
                stage,
                repo=repo,
                block_state=block_state,
                new_commit_oid=new_commit_oid,
                tag_object_oid=tag_object_oid,
                main_branch=main_branch,
                main_oid_before=main_oid_before,
                tag_name=tag_name,
                exc=exc,
            ),
            stage=stage,
            tag_object_oid=tag_object_oid,
        ) from exc

    # Branch retention (research.md §15, T064-T066) belongs here in a later
    # phase; intentionally not stubbed. No push, ever (research.md §0.2).
    return CheckpointOutcome(tag_name=tag_name, checkpoint_commit_oid=new_commit_oid, merge_commit_sha=merge_commit_sha)


def _verify_tag_object(
    repo: GitRepo,
    *,
    tag_object_oid: str,
    expected_payload: str,
    tag_name: str,
    merge_commit_sha: str,
    tag_message: str,
    main_branch: str,
) -> None:
    """B3B: verify the unreferenced tag object by its exact OID before any
    ref can name it."""
    if repo.object_type(tag_object_oid) != "tag":
        raise WorkflowError(f"object {tag_object_oid!r} is not a tag object")
    actual_payload = repo.read_tag_object(tag_object_oid)
    if actual_payload != expected_payload:
        raise WorkflowError(f"tag object {tag_object_oid!r} content does not match the expected payload")
    header, sep, body = actual_payload.partition("\n\n")
    header_lines = header.split("\n")
    if (
        not sep
        or len(header_lines) < 4
        or header_lines[0] != f"object {merge_commit_sha}"
        or header_lines[1] != "type commit"
        or header_lines[2] != f"tag {tag_name}"
        or not header_lines[3].startswith("tagger ")
    ):
        raise WorkflowError(f"tag object {tag_object_oid!r} headers do not match the expected target/type/name")
    if body != tag_message or "\nCheckpoint: VERIFIED\n" not in "\n" + body:
        raise WorkflowError(f"tag object {tag_object_oid!r} annotation does not match the expected content")
    if repo.rev_parse(f"{tag_object_oid}^{{commit}}") != merge_commit_sha:
        raise WorkflowError(f"tag object {tag_object_oid!r} does not peel to {merge_commit_sha!r}")
    if repo.branch_oid(main_branch) != merge_commit_sha:
        raise WorkflowError(f"{main_branch!r} moved away from the verified merge commit before tag publication")


def _observe(label: str, probe) -> str:
    try:
        return f"{label}={probe()}"
    except Exception as exc:  # noqa: BLE001 - observation is best-effort
        return f"{label}=unknown ({exc})"


_ENVELOPE_STAGE_ORDER: tuple[str, ...] = (
    STAGE_INDEX_STAGING_STARTED,
    STAGE_INDEX_STAGED,
    STAGE_COMMIT_OBJECT_CREATE_ATTEMPTED,
    STAGE_COMMIT_OBJECT_CREATED,
    STAGE_BRANCH_REF_UPDATE_ATTEMPTED,
    STAGE_BRANCH_REF_ADVANCED,
    STAGE_MAIN_SWITCH_ATTEMPTED,
    STAGE_MERGE_ATTEMPTED,
    STAGE_MERGE_VERIFIED,
    STAGE_TAG_OBJECT_CREATE_ATTEMPTED,
    STAGE_TAG_OBJECT_CREATED,
    STAGE_TAG_OBJECT_VERIFIED,
    STAGE_TAG_REF_PUBLISH_ATTEMPTED,
    STAGE_TAG_REF_PUBLISHED,
)


def _commit_object_observation(stage: str, new_commit_oid: str | None) -> str:
    """Fifth remediation (M1): never claim a commit object is absent."""
    if new_commit_oid is not None:
        return f"checkpoint commit object={new_commit_oid} (reported)"
    if _ENVELOPE_STAGE_ORDER.index(stage) < _ENVELOPE_STAGE_ORDER.index(STAGE_COMMIT_OBJECT_CREATE_ATTEMPTED):
        return "checkpoint commit object: commit-tree NOT attempted"
    return "checkpoint commit object: no OID reported (creation attempted, NOT proven; an unreferenced object MAY exist)"


def _tag_object_observation(stage: str, tag_object_oid: str | None) -> str:
    if tag_object_oid is None:
        return "tag object OID: none reported"
    if stage == STAGE_TAG_OBJECT_CREATED:
        return f"mktag reported tag object OID={tag_object_oid} (verification/correctness NOT proven)"
    return f"mktag reported tag object OID={tag_object_oid} (exact verification completed)"


def _partial_failure_message(
    stage: str,
    *,
    repo: GitRepo,
    block_state: BlockState,
    new_commit_oid: str | None,
    tag_object_oid: str | None,
    main_branch: str,
    main_oid_before: str,
    tag_name: str,
    exc: Exception,
) -> str:
    """M5: the stage's proven facts, plus read-only observations taken now
    (never assumed)."""
    observations = "; ".join(
        [
            _observe(f"refs/heads/{block_state.branch_name}", lambda: repo.rev_parse(f"refs/heads/{block_state.branch_name}")),
            _observe(f"refs/heads/{main_branch}", lambda: repo.rev_parse(f"refs/heads/{main_branch}")),
            _observe(f"refs/tags/{tag_name} exists", lambda: repo.tag_exists(tag_name)),
            _commit_object_observation(stage, new_commit_oid),
            _tag_object_observation(stage, tag_object_oid),
            f"main before checkpoint={main_oid_before}",
        ]
    )
    return (
        f"CHECKPOINT PARTIALLY FAILED at mutation boundary {stage!r} — {_STAGE_FACTS[stage]}. "
        f"Observed now: {observations}. Manual recovery is required; this state is never "
        f"auto-retried or auto-repaired. Underlying error: {exc}"
    )


def status_preview(repo: GitRepo, config: WorkflowConfig, block_state: BlockState, *, ai_runs_dir: Path) -> str:
    """M4 (second round): a genuinely READ-ONLY answer to "would checkpoint
    currently be blocked?". Never builds a Candidate Tree (that writes Git
    objects); the fingerprint recheck is explicitly deferred to
    `checkpoint`. Never raises."""
    if block_state.checkpoint_mutation_boundary is not None:
        return (
            "blocked (a previous checkpoint attempt failed after mutating Git state at boundary "
            f"{block_state.checkpoint_mutation_boundary!r}; manual recovery required)"
        )
    if block_state.state not in _CHECKPOINT_ELIGIBLE_STATES:
        return (
            f"blocked (block-state is {block_state.state!r}, which is never checkpoint-eligible "
            "regardless of any cached gate data)"
        )

    branch_name = block_state.branch_name
    try:
        current = repo.current_branch()
    except WorkflowError as exc:
        return f"blocked (could not determine current branch: {exc})"
    if current != branch_name:
        return f"blocked (current branch {current!r} does not match the active block's branch {branch_name!r})"
    if branch_name == config.project.main_branch:
        return f"blocked (current branch is the main branch {config.project.main_branch!r})"

    try:
        repo.check_staging_precondition()
    except WorkflowError as exc:
        return f"blocked ({exc})"

    if block_state.last_gate is None or block_state.last_safe_stage != "GATE_PASSED":
        return f"blocked ({_no_eligible_gate_message(block_state)})"

    try:
        _verify_gate_evidence(block_state, ai_runs_dir)
    except CheckpointBlockedError as exc:
        return f"blocked ({exc.message})"

    try:
        current_main_oid = repo.branch_oid(config.project.main_branch)
    except WorkflowError as exc:
        return f"blocked (could not resolve {config.project.main_branch!r}: {exc})"
    if current_main_oid != block_state.gated_main_oid:
        return f"blocked ({CHECKPOINT_MAIN_ADVANCED_MESSAGE})"
    if not (
        block_state.expected_head_oid is not None
        and block_state.last_gate.head_oid == block_state.expected_head_oid == block_state.gated_main_oid
    ):
        return f"blocked ({CHECKPOINT_BASE_MISMATCH_MESSAGE})"

    tag_name = build_checkpoint_tag_name(block_state.first_task, block_state.last_task)
    try:
        if repo.tag_exists(tag_name):
            return f"blocked (tag {tag_name!r} already exists)"
    except WorkflowError as exc:
        return f"blocked (could not check tag existence: {exc})"

    return (
        "preconditions that can be checked without mutating Git metadata all look satisfied "
        "(lifecycle state, branch, staging area, immutable gate evidence, base identity, no tag "
        "collision); the Candidate Tree/Fingerprint recheck itself can only be performed by "
        "actually running 'checkpoint' (building it would itself write a Git object)"
    )
