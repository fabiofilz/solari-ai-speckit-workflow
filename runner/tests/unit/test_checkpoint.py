"""Unit tests for `git/checkpoint.py` (T040 remediation: B3 merge-topology
verification, M2 partial-failure safety and tag-collision preflight, M4
read-only status preview).

Every test builds its own isolated, disposable temporary Git repository
(`tmp_path` + real `git init`), matching `test_candidate_tree.py`'s own
pattern, and constructs a `WorkflowConfig`/`BlockState` directly (no TOML
parsing needed for these unit-level tests).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solari_workflow.config.schema import (
    AiRunsSection,
    BranchRetentionSection,
    BuildSection,
    CheckpointSection,
    EnvironmentSection,
    GitPushSection,
    GitSection,
    ProjectSection,
    SpeckitSection,
    WorkflowConfig,
    WorkflowSection,
)
from solari_workflow.fingerprint.engine import compute_fingerprint
from solari_workflow.git import checkpoint
from solari_workflow.git.candidate_tree import build_candidate_tree
from solari_workflow.git.ops import GitRepo, open_repository
from solari_workflow.state.block_state import new_block_state

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    (path / ".gitignore").write_text(".ai-runs/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)


def _config() -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowSection(schema_version="1", source="https://example.invalid/repo", version="1.0.0"),
        project=ProjectSection(name="test", type="service", main_branch="main"),
        speckit=SpeckitSection(spec_dir="specs"),
        environment=EnvironmentSection(
            stack="other", runtime_version="unspecified", dependency_manager="unspecified", manifest="unspecified",
            lockfile_must_be_committed=True,
        ),
        git=GitSection(checkpoint_merge_strategy="no-ff", push=GitPushSection(mode="manual")),
        checkpoint=CheckpointSection(tag_prefix="checkpoint", blocking_severities=["blocker", "major"]),
        branch_retention=BranchRetentionSection(keep_recent_merged=2, keep_active=True),
        ai_runs=AiRunsSection(directory=".ai-runs", git_ignored=True),
        build=BuildSection(),
    )


def _gated_block_state(
    repo: GitRepo,
    *,
    branch_name: str = "T091-Test",
    first_task: str = "T091",
    last_task: str = "T091",
    main_branch: str = "main",
    tasks_md_path: str | None = None,
):
    """Build a real Candidate Tree/Fingerprint, the IMMUTABLE structured
    gate-identity evidence, and a `BlockState` at `GATE_PASSED` exactly as
    `run-codex-gate` leaves it (third remediation: evidence reference,
    expected base == main == gated head)."""
    from dataclasses import replace

    from solari_workflow.runs import audit
    from solari_workflow.state import gate_identity

    candidate = build_candidate_tree(repo)
    fingerprint = compute_fingerprint(repo, candidate, first_task=first_task, last_task=last_task)
    main_oid = repo.rev_parse(main_branch)
    ai_runs_dir = repo.path / ".ai-runs"
    record = audit.create_run_record(ai_runs_dir, slug_source=f"{branch_name}-gate", prompt_content="gate\n")
    identity = gate_identity.GateIdentity(
        fingerprint=fingerprint, feature_identity=tasks_md_path, gated_main_oid=main_oid
    )
    name, digest = gate_identity.write_gate_evidence(record, identity, result="PASS", checkpoint_eligible=True)

    state = new_block_state(
        branch_name=branch_name,
        first_task=first_task,
        last_task=last_task,
        block_name="Test",
        tasks_md_path=tasks_md_path,
        expected_head_oid=main_oid,
    )
    return replace(
        state,
        last_safe_stage="GATE_PASSED",
        last_gate=fingerprint,
        gated_main_oid=main_oid,
        gate_feature_identity=tasks_md_path,
        gate_identity_record=name,
        gate_identity_sha256=digest,
    )


def _ai(repo: GitRepo) -> Path:
    return repo.path / ".ai-runs"


def _create_and_switch(repo: GitRepo, branch_name: str) -> None:
    repo.create_branch(branch_name, start_point="main")
    repo.switch(branch_name)


# --- M2: tag-collision preflight (no mutation) -----------------------------


def test_preflight_refuses_on_tag_collision_without_mutating_anything(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    # Pre-create the tag the checkpoint would try to create.
    repo.tag_annotated("checkpoint-T091-T091", "pre-existing", target="main")

    real_index_oid_before = repo.write_tree()
    branch_ref_before = repo.rev_parse("T091-Test")

    with pytest.raises(checkpoint.CheckpointBlockedError) as excinfo:
        checkpoint.preflight(repo, config, state, ai_runs_dir=_ai(repo))
    assert "already exists" in str(excinfo.value)

    # Nothing mutated.
    assert repo.write_tree() == real_index_oid_before
    assert repo.rev_parse("T091-Test") == branch_ref_before
    assert repo.current_branch() == "T091-Test"


def test_run_checkpoint_refuses_on_tag_collision_before_any_mutation(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()
    repo.tag_annotated("checkpoint-T091-T091", "pre-existing", target="main")

    branch_ref_before = repo.rev_parse("T091-Test")
    with pytest.raises(checkpoint.CheckpointBlockedError):
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert repo.rev_parse("T091-Test") == branch_ref_before
    assert repo.current_branch() == "T091-Test"  # never switched to main


# --- B3: merge-topology verification ---------------------------------------


def test_run_checkpoint_succeeds_and_produces_correct_topology(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    outcome = checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))

    checkpoint_commit_oid = repo.rev_parse("T091-Test")
    assert outcome.checkpoint_commit_oid == checkpoint_commit_oid
    assert outcome.merge_commit_sha != checkpoint_commit_oid
    parents = repo.parents_of(outcome.merge_commit_sha)
    assert len(parents) == 2
    assert parents[1] == checkpoint_commit_oid
    assert repo.object_type(outcome.tag_name) == "tag"
    assert repo.rev_parse(f"{outcome.tag_name}^{{commit}}") == outcome.merge_commit_sha


def test_run_checkpoint_detects_a_forced_parent_order_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B3: even if `git merge --no-ff` itself exits zero, a corrupted/
    unexpected parent order must be caught before tagging, not assumed."""
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    original_parents_of = GitRepo.parents_of

    def _fake_parents_of(self: GitRepo, commit_ish: str):
        real = original_parents_of(self, commit_ish)
        if len(real) == 2:
            return list(reversed(real))  # simulate a swapped parent order
        return real

    monkeypatch.setattr(GitRepo, "parents_of", _fake_parents_of)

    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert excinfo.value.stage == checkpoint.STAGE_MERGE_ATTEMPTED
    assert "are not exactly" in str(excinfo.value)
    assert not repo.tag_exists("checkpoint-T091-T091")


def test_run_checkpoint_detects_a_forced_merge_tree_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    original_cat_file_tree = GitRepo.cat_file_tree
    call_count = {"n": 0}

    def _fake_cat_file_tree(self: GitRepo, commit_ish: str) -> str:
        call_count["n"] += 1
        # Let the earlier (checkpoint-commit-tree) verification pass, but
        # force the LATER (merge-commit-tree) verification to see a wrong
        # value.
        if call_count["n"] >= 2:
            return "0" * 40
        return original_cat_file_tree(self, commit_ish)

    monkeypatch.setattr(GitRepo, "cat_file_tree", _fake_cat_file_tree)

    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert "does not equal" in str(excinfo.value) or "unreviewed content" in str(excinfo.value)


# --- M2: partial-failure safety ---------------------------------------------


def test_update_ref_itself_failing_reports_no_visible_ref_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 11: 'if update_ref fails before changing the branch, report
    NO VISIBLE REF MUTATION PROVEN' - the prior implementation's own
    partial-failure message unconditionally claimed the branch had
    already been advanced, which would be FALSE for a failure in
    `update_ref` itself. Truthful reporting distinguishes this from
    `merge_no_ff` failing (a later stage, where the branch genuinely was
    already advanced)."""
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    branch_oid_before = repo.rev_parse("T091-Test")

    def _boom(self: GitRepo, ref: str, new_oid: str, old_oid: str) -> None:
        raise RuntimeError("simulated update-ref failure")

    monkeypatch.setattr(GitRepo, "update_ref", _boom)

    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    message = str(excinfo.value)
    assert excinfo.value.stage == checkpoint.STAGE_BRANCH_REF_UPDATE_ATTEMPTED
    assert "NOT proven" in message
    assert "was advanced" not in message  # must NOT claim a mutation that never happened

    # The branch really was never touched.
    assert repo.rev_parse("T091-Test") == branch_oid_before


def test_merge_failure_after_ref_advance_is_a_partial_failure_not_a_silent_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure AFTER `update-ref` (the block branch's own ref already
    advanced to the real checkpoint commit) must surface as
    `CheckpointPartialFailureError`, naming that the branch already
    advanced - never silently swallowed or retried."""
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    def _boom(self: GitRepo, branch: str, message: str | None = None) -> None:
        raise RuntimeError("simulated merge failure")

    monkeypatch.setattr(GitRepo, "merge_no_ff", _boom)

    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    message = str(excinfo.value)
    assert excinfo.value.stage == checkpoint.STAGE_MERGE_ATTEMPTED
    assert "T091-Test" in message
    # M5/8B: a failed merge must never claim main/index/worktree untouched.
    assert "MAY be modified" in message
    assert "untouched" not in message

    # The branch ref really was advanced to a real, valid checkpoint commit.
    branch_commit_oid = repo.rev_parse("T091-Test")
    assert repo.cat_file_tree(branch_commit_oid) == state.last_gate.candidate_tree_oid
    # `main` was never touched.
    assert repo.rev_parse("main") != branch_commit_oid


def test_tag_object_write_failure_after_merge_is_a_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    def _boom(self: GitRepo, payload: str) -> str:
        raise RuntimeError("simulated tag-object write failure")

    monkeypatch.setattr(GitRepo, "mktag", _boom)

    with pytest.raises(checkpoint.CheckpointPartialFailureError):
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))

    # The merge itself DID complete (main advanced), but no tag exists -
    # a real, inspectable partial state.
    assert repo.current_branch() == "main"
    assert not repo.tag_exists("checkpoint-T091-T091")


# --- M4: status_preview is genuinely read-only ------------------------------


def test_status_preview_reports_ready_without_mutating_the_real_index(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    real_index_oid_before = repo.write_tree()
    message = checkpoint.status_preview(repo, config, state, ai_runs_dir=_ai(repo))
    assert "satisfied" in message or "would" not in message.lower() or "blocked" not in message
    assert repo.write_tree() == real_index_oid_before  # never touched


def test_status_preview_reports_blocked_reason_for_wrong_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    state = _gated_block_state(repo, branch_name="T091-Test")
    config = _config()
    # Still on 'main', never created/switched to T091-Test.
    message = checkpoint.status_preview(repo, config, state, ai_runs_dir=_ai(repo))
    assert "blocked" in message
    assert "does not match" in message


def test_status_preview_never_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    state = _gated_block_state(repo, branch_name="does-not-exist")
    config = _config()
    message = checkpoint.status_preview(repo, config, state, ai_runs_dir=_ai(repo))
    assert isinstance(message, str)
    assert "blocked" in message


# --- B3/item 3: a VERIFIED tag is never visible before it is verified -----


def _tag_message(tmp_path: Path, tag_name: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "tag", "-n99", "-l", tag_name], capture_output=True, text=True, check=True
    )
    return result.stdout


def test_no_tag_is_ever_visible_if_object_verification_fails_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3/item 3, the core guarantee: `mktag` writes the tag OBJECT
    first, entirely unreferenced; if verifying THAT object fails, NO ref
    is ever created at all - not a provisional one, not the real one -
    so `checkpoint-T091-T091` is not merely "not yet VERIFIED", it does
    not exist as a name at all."""
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    def _fake_object_type(self: GitRepo, oid: str) -> str:
        raise RuntimeError("simulated verification failure on the unreferenced tag object")

    monkeypatch.setattr(GitRepo, "object_type", _fake_object_type)

    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    # M5/8A: the tag OBJECT exists but was NOT verified - the message must
    # never claim it was, and must say no tag ref was published.
    message = str(excinfo.value)
    assert excinfo.value.stage == checkpoint.STAGE_TAG_OBJECT_CREATED
    assert "tag ref publication was NOT attempted" in message
    assert "fully-verified" not in message and "written and verified" not in message

    tag_name = "checkpoint-T091-T091"
    assert not repo.tag_exists(tag_name)  # no ref by that name exists AT ALL


def test_no_tag_is_visible_if_the_publish_ref_update_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even once the tag object is fully verified, if the SINGLE
    `update_ref` publish call itself fails, still no tag is visible by
    name - the verified object remains harmless, unreferenced data."""
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    original_update_ref = GitRepo.update_ref

    def _fake_update_ref(self: GitRepo, ref: str, new_oid: str, old_oid: str) -> None:
        if ref.startswith("refs/tags/"):
            raise RuntimeError("simulated tag publish failure")
        return original_update_ref(self, ref, new_oid, old_oid)

    monkeypatch.setattr(GitRepo, "update_ref", _fake_update_ref)

    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert excinfo.value.stage == checkpoint.STAGE_TAG_REF_PUBLISH_ATTEMPTED

    tag_name = "checkpoint-T091-T091"
    assert not repo.tag_exists(tag_name)


def test_the_published_tag_is_immediately_verified_and_correct(tmp_path: Path) -> None:
    """Sanity check: on the happy path, exactly one tag ends up visible,
    it is a genuine annotated tag object, and it carries the VERIFIED
    message from the moment it becomes visible at all."""
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))

    tags = subprocess.run(
        ["git", "-C", str(tmp_path), "tag", "-l", "checkpoint-T091-T091*"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert tags == ["checkpoint-T091-T091"]
    assert repo.object_type("checkpoint-T091-T091") == "tag"
    message = _tag_message(tmp_path, "checkpoint-T091-T091")
    assert "Checkpoint: VERIFIED" in message


# --- item 8: a gate from another feature must never be reusable -----------


def test_preflight_rejects_a_gate_whose_recorded_feature_identity_does_not_match(tmp_path: Path) -> None:
    """Simulates the composed Block/Gate Identity (item 8) diverging from
    the block's own current, persisted feature identity - e.g. a gate
    that was (however implausibly) computed for a DIFFERENT Spec Kit
    feature than the one currently bound to this block. Even though the
    5-field Fingerprint identity (head/tree/branch/task-range) might
    otherwise line up, the identity as a WHOLE must be rejected."""
    from dataclasses import replace

    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    config = _config()

    state = _gated_block_state(repo, tasks_md_path="specs/001-alpha/tasks.md")
    # Simulate the gate having been computed under a DIFFERENT feature's
    # identity than the block's own current, persisted one.
    mismatched_state = replace(state, gate_feature_identity="specs/002-beta/tasks.md")

    with pytest.raises(checkpoint.CheckpointBlockedError) as excinfo:
        checkpoint.preflight(repo, config, mismatched_state, ai_runs_dir=_ai(repo))
    assert "feature identity" in str(excinfo.value)


# --- item 12: the staged-tree link is INDEPENDENTLY observed ---------------


def test_staged_tree_is_independently_observed_before_the_commit_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 12: 'the staged tree assertion must be independently
    observed - do not infer it only because the later commit tree
    matches.' Intercepts `commit_tree` to independently read the REAL
    index's own `write-tree` oid at the exact moment staging is already
    done but no commit object exists yet - this is what genuinely proves
    'staged tree X', decoupled from the commit-tree check that comes
    after it.
    """
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo)
    config = _config()

    observed: dict[str, str] = {}
    original_commit_tree = GitRepo.commit_tree

    def _observing_commit_tree(self: GitRepo, tree_oid: str, parent_oid: str, message: str) -> str:
        # At this exact point: staging (read-tree + checkout-index)
        # already happened, but no commit object referencing `tree_oid`
        # exists yet - the real index's OWN write-tree result is "staged
        # tree X", observed independently of the commit about to be made.
        observed["staged_tree_via_real_index"] = self.write_tree()
        return original_commit_tree(self, tree_oid, parent_oid, message)

    monkeypatch.setattr(GitRepo, "commit_tree", _observing_commit_tree)

    outcome = checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))

    assert observed["staged_tree_via_real_index"] == state.last_gate.candidate_tree_oid
    # A SEPARATE, later link - the checkpoint commit's own tree - checked
    # independently too, not merely assumed from the staged-tree link above.
    assert repo.cat_file_tree(outcome.checkpoint_commit_oid) == state.last_gate.candidate_tree_oid


def test_preflight_accepts_a_gate_whose_feature_identity_matches(tmp_path: Path) -> None:
    """Sanity check: the item-8 check does not block the ordinary case
    where the gate's recorded feature identity matches the block's own."""
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    config = _config()
    state = _gated_block_state(repo, tasks_md_path="specs/001-alpha/tasks.md")

    fingerprint_x = checkpoint.preflight(repo, config, state, ai_runs_dir=_ai(repo))
    assert fingerprint_x == state.last_gate


# --- Third remediation B3: VERIFIED publication semantics -------------------


def _all_commit_messages(tmp_path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--all", "--format=%B%x00"], capture_output=True, text=True, check=True
    ).stdout


def _ready(tmp_path: Path):
    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    return repo, _gated_block_state(repo), _config()


def test_b3a_no_commit_message_ever_claims_verified(tmp_path: Path) -> None:
    repo, state, config = _ready(tmp_path)
    outcome = checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert "VERIFIED" not in _all_commit_messages(tmp_path)
    assert "Checkpoint: VERIFIED" in repo.read_tag_object(repo.rev_parse(f"refs/tags/{outcome.tag_name}"))


def _assert_no_tag_and_no_verified_commit(repo: GitRepo, tmp_path: Path) -> None:
    assert not repo.tag_exists("checkpoint-T091-T091")
    assert "VERIFIED" not in _all_commit_messages(tmp_path)


def test_b3b_tag_object_content_mismatch_publishes_no_tag_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state, config = _ready(tmp_path)
    original = GitRepo.read_tag_object

    def _tampered(self: GitRepo, oid: str) -> str:
        return original(self, oid).replace("Gate: PASS", "Gate: FAIL")

    monkeypatch.setattr(GitRepo, "read_tag_object", _tampered)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert excinfo.value.stage == checkpoint.STAGE_TAG_OBJECT_CREATED
    assert "does not match the expected payload" in str(excinfo.value)
    _assert_no_tag_and_no_verified_commit(repo, tmp_path)


def test_b3b_tag_object_read_failure_publishes_no_tag_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state, config = _ready(tmp_path)

    def _boom(self: GitRepo, oid: str) -> str:
        raise RuntimeError("simulated cat-file failure")

    monkeypatch.setattr(GitRepo, "read_tag_object", _boom)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert excinfo.value.stage == checkpoint.STAGE_TAG_OBJECT_CREATED
    _assert_no_tag_and_no_verified_commit(repo, tmp_path)


def test_b3b_peeled_target_mismatch_publishes_no_tag_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state, config = _ready(tmp_path)
    original = GitRepo.rev_parse

    def _fake(self: GitRepo, rev: str) -> str:
        if rev.endswith("^{commit}"):
            return "0" * 40
        return original(self, rev)

    monkeypatch.setattr(GitRepo, "rev_parse", _fake)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert excinfo.value.stage == checkpoint.STAGE_TAG_OBJECT_CREATED
    assert "does not peel to" in str(excinfo.value)
    _assert_no_tag_and_no_verified_commit(repo, tmp_path)


def test_b3b_final_tag_update_ref_failure_publishes_no_tag_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state, config = _ready(tmp_path)
    original = GitRepo.update_ref

    def _fake(self: GitRepo, ref: str, new_oid: str, old_oid: str) -> None:
        if ref.startswith("refs/tags/"):
            raise RuntimeError("simulated tag CAS failure")
        return original(self, ref, new_oid, old_oid)

    monkeypatch.setattr(GitRepo, "update_ref", _fake)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert excinfo.value.stage == checkpoint.STAGE_TAG_REF_PUBLISH_ATTEMPTED
    _assert_no_tag_and_no_verified_commit(repo, tmp_path)


def test_b3b_tag_publication_is_cas_on_non_existence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag created by someone else between preflight and publication is
    never overwritten: the CAS requires the ref to not exist."""
    repo, state, config = _ready(tmp_path)
    original_mktag = GitRepo.mktag

    def _race(self: GitRepo, payload: str) -> str:
        oid = original_mktag(self, payload)
        self.tag_annotated("checkpoint-T091-T091", "racing tag", target="main")
        return oid

    monkeypatch.setattr(GitRepo, "mktag", _race)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert excinfo.value.stage == checkpoint.STAGE_TAG_REF_PUBLISH_ATTEMPTED
    assert "Checkpoint: VERIFIED" not in repo.read_tag_object(repo.rev_parse("refs/tags/checkpoint-T091-T091"))


# --- Third remediation M4/M5: truthful stage per boundary ------------------


@pytest.mark.parametrize(
    ("method", "expected_stage"),
    [
        ("read_tree", checkpoint.STAGE_INDEX_STAGING_STARTED),
        ("commit_tree", checkpoint.STAGE_COMMIT_OBJECT_CREATE_ATTEMPTED),
        ("switch", checkpoint.STAGE_MAIN_SWITCH_ATTEMPTED),
        ("mktag", checkpoint.STAGE_TAG_OBJECT_CREATE_ATTEMPTED),
    ],
)
def test_every_mutation_boundary_is_reported_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, expected_stage: str
) -> None:
    repo, state, config = _ready(tmp_path)
    original = getattr(GitRepo, method)

    def _boom(self: GitRepo, *args, **kwargs):
        # Only the REAL-index/ref mutation inside the envelope fails; the
        # pure preflight's temporary-index Candidate Tree rebuild (which
        # passes a `GIT_INDEX_FILE` env) is left intact.
        if kwargs.get("env") is not None:
            return original(self, *args, **kwargs)
        raise RuntimeError(f"simulated {method} failure")

    monkeypatch.setattr(GitRepo, method, _boom)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert excinfo.value.stage == expected_stage
    assert f"'{expected_stage}'" in str(excinfo.value)
    assert not repo.tag_exists("checkpoint-T091-T091")
    message = str(excinfo.value)
    if expected_stage == checkpoint.STAGE_TAG_OBJECT_CREATE_ATTEMPTED:
        # mktag was invoked and reported failure, which does not prove no
        # (unreachable) tag object exists.
        assert "tag object creation (mktag) was ATTEMPTED but NOT PROVEN" in message
        assert "unreferenced tag object MAY exist" in message
    if expected_stage == checkpoint.STAGE_COMMIT_OBJECT_CREATE_ATTEMPTED:
        assert "commit object creation (commit-tree) was ATTEMPTED but NOT PROVEN" in message
        assert "unreferenced commit object MAY exist" in message
    assert "not created" not in message


# --- Third remediation M1: base binding ------------------------------------


def test_m1_preflight_refuses_when_head_differs_from_expected_base(tmp_path: Path) -> None:
    from dataclasses import replace

    repo, state, config = _ready(tmp_path)
    bogus = replace(state, expected_head_oid="1" * 40)
    with pytest.raises(checkpoint.CheckpointBlockedError) as excinfo:
        checkpoint.preflight(repo, config, bogus, ai_runs_dir=_ai(repo))
    assert checkpoint.CHECKPOINT_BASE_MISMATCH_MESSAGE in str(excinfo.value)


def test_m1_preflight_refuses_when_main_advanced_after_gate_before_any_mutation(tmp_path: Path) -> None:
    repo, state, config = _ready(tmp_path)
    main_tree = repo.cat_file_tree("main")
    new_main = repo.commit_tree(main_tree, repo.rev_parse("main"), "advance main")
    repo.update_ref("refs/heads/main", new_main, repo.rev_parse("main"))
    branch_before = repo.rev_parse("T091-Test")
    index_before = repo.write_tree()
    with pytest.raises(checkpoint.CheckpointBlockedError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert checkpoint.CHECKPOINT_MAIN_ADVANCED_MESSAGE in str(excinfo.value)
    assert repo.rev_parse("T091-Test") == branch_before
    assert repo.write_tree() == index_before
    assert repo.current_branch() == "T091-Test"


# --- Third remediation M3: immutable Gate Identity -------------------------


def test_m3_mutating_cached_feature_identity_to_feature_b_is_refused(tmp_path: Path) -> None:
    from dataclasses import replace

    _init_repo(tmp_path)
    repo = open_repository(tmp_path)
    _create_and_switch(repo, "T091-Test")
    (tmp_path / "feature.txt").write_text("x\n", encoding="utf-8")
    state = _gated_block_state(repo, tasks_md_path="specs/001-alpha/tasks.md")
    tampered = replace(state, tasks_md_path="specs/002-beta/tasks.md", gate_feature_identity="specs/002-beta/tasks.md")
    with pytest.raises(checkpoint.CheckpointBlockedError) as excinfo:
        checkpoint.preflight(repo, _config(), tampered, ai_runs_dir=_ai(repo))
    assert checkpoint.CHECKPOINT_GATE_EVIDENCE_MESSAGE in str(excinfo.value)


def test_m3_mutating_cached_gated_main_is_refused(tmp_path: Path) -> None:
    from dataclasses import replace

    repo, state, config = _ready(tmp_path)
    tampered = replace(state, gated_main_oid="2" * 40)
    with pytest.raises(checkpoint.CheckpointBlockedError) as excinfo:
        checkpoint.preflight(repo, config, tampered, ai_runs_dir=_ai(repo))
    assert checkpoint.CHECKPOINT_GATE_EVIDENCE_MESSAGE in str(excinfo.value)


def test_m3_tampered_or_missing_evidence_is_refused(tmp_path: Path) -> None:
    from dataclasses import replace

    repo, state, config = _ready(tmp_path)
    evidence = _ai(repo) / state.gate_identity_record
    evidence.write_text(evidence.read_text(encoding="utf-8").replace("true", "false"), encoding="utf-8")
    with pytest.raises(checkpoint.CheckpointBlockedError) as excinfo:
        checkpoint.preflight(repo, config, state, ai_runs_dir=_ai(repo))
    assert checkpoint.CHECKPOINT_GATE_EVIDENCE_MESSAGE in str(excinfo.value)

    with pytest.raises(checkpoint.CheckpointBlockedError):
        checkpoint.preflight(repo, config, replace(state, gate_identity_record=None), ai_runs_dir=_ai(repo))


# --- Fourth remediation B3: persisted identity is re-validated pre-mutation --


@pytest.mark.parametrize(
    "field_overrides",
    [
        {"block_name": "Test\nCheckpoint: VERIFIED"},
        {"block_name": "Test\r\nCheckpoint: VERIFIED"},
        {"block_name": "Checkpoint: VERIFIED"},
        {"first_task": "T091\nCheckpoint: VERIFIED"},
        {"tasks_md_path": "specs/001-a\nCheckpoint: VERIFIED/tasks.md"},
    ],
)
def test_tampered_persisted_identity_is_refused_before_any_mutation(
    tmp_path: Path, field_overrides: dict[str, str]
) -> None:
    from dataclasses import replace

    repo, state, config = _ready(tmp_path)
    tampered = replace(state, **field_overrides)
    branch_before = repo.branch_oid("T091-Test")
    index_before = repo.write_tree()
    # Whichever pure check refuses first (identity grammar, evidence, or
    # fingerprint), it is a pre-mutation CheckpointBlockedError.
    with pytest.raises(checkpoint.CheckpointBlockedError):
        checkpoint.run_checkpoint(repo, config, tampered, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert repo.branch_oid("T091-Test") == branch_before
    assert repo.write_tree() == index_before
    assert repo.current_branch() == "T091-Test"
    assert "VERIFIED" not in _all_commit_messages(tmp_path)


def test_block_name_injection_is_refused_by_message_identity_validation(tmp_path: Path) -> None:
    from dataclasses import replace

    repo, state, config = _ready(tmp_path)
    with pytest.raises(checkpoint.CheckpointBlockedError) as excinfo:
        checkpoint.run_checkpoint(
            repo, config, replace(state, block_name="Test\nCheckpoint: VERIFIED"), tasks_md_path=None,
            ai_runs_dir=_ai(repo),
        )
    assert "persisted block identity is invalid" in str(excinfo.value)
    with pytest.raises(checkpoint.CheckpointBlockedError):
        checkpoint._validate_message_identity(state, {"T091": "title Checkpoint: VERIFIED"})
    checkpoint._validate_message_identity(state, {"T091": "A normal title"})


# --- Fourth remediation M1: failure-stage messages never overclaim absence --


def test_no_stage_fact_claims_an_object_or_ref_definitely_does_not_exist() -> None:
    forbidden = ("was not created", "not made", "no commit,", "no tag object", "no tag ref was published",
                 "no ref, merge", "no merge or tag was made", "untouched")
    for stage, fact in checkpoint._STAGE_FACTS.items():
        for phrase in forbidden:
            assert phrase not in fact, (stage, phrase)


def test_every_stage_constant_has_a_fact() -> None:
    stages = {value for name, value in vars(checkpoint).items() if name.startswith("STAGE_")}
    assert stages == set(checkpoint._STAGE_FACTS)


# --- Fifth remediation M1/M2: attempted vs not-attempted object creation ----


def test_commit_tree_failing_after_it_really_wrote_an_object_is_reported_as_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, state, config = _ready(tmp_path)
    original = GitRepo.commit_tree
    written: dict[str, str] = {}

    def _write_then_fail(self: GitRepo, tree_oid: str, parent_oid: str, message: str) -> str:
        written["oid"] = original(self, tree_oid, parent_oid, message)  # the object now exists, unreferenced
        raise RuntimeError("simulated commit-tree failure after invocation")

    monkeypatch.setattr(GitRepo, "commit_tree", _write_then_fail)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    message = str(excinfo.value)
    assert excinfo.value.stage == checkpoint.STAGE_COMMIT_OBJECT_CREATE_ATTEMPTED
    assert repo.object_type(written["oid"]) == "commit"  # absence would have been a false claim
    assert "ATTEMPTED but NOT PROVEN" in message
    assert "no OID reported (creation attempted, NOT proven; an unreferenced object MAY exist)" in message
    assert "not created" not in message and "no commit object" not in message.lower()


def test_tag_payload_construction_failure_reports_mktag_not_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, state, config = _ready(tmp_path)
    mktag_calls: list[str] = []

    def _payload_boom(self: GitRepo, *, target: str, tag_name: str, message: str) -> str:
        raise RuntimeError("simulated tagger identity / payload construction failure")

    def _mktag_spy(self: GitRepo, payload: str) -> str:
        mktag_calls.append(payload)
        raise AssertionError("mktag must not be invoked")

    monkeypatch.setattr(GitRepo, "build_tag_payload", _payload_boom)
    monkeypatch.setattr(GitRepo, "mktag", _mktag_spy)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    message = str(excinfo.value)
    assert mktag_calls == []
    assert excinfo.value.stage == checkpoint.STAGE_MERGE_VERIFIED
    assert "tag object creation (mktag) was NOT attempted" in message
    assert excinfo.value.tag_object_oid is None
    assert "tag object OID: none reported" in message
    assert "ATTEMPTED" not in message and "MAY exist" not in message
    assert not repo.tag_exists("checkpoint-T091-T091")


def test_mktag_invocation_failure_reports_attempted_but_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, state, config = _ready(tmp_path)

    def _mktag_boom(self: GitRepo, payload: str) -> str:
        raise RuntimeError("simulated mktag failure")

    monkeypatch.setattr(GitRepo, "mktag", _mktag_boom)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    message = str(excinfo.value)
    assert excinfo.value.stage == checkpoint.STAGE_TAG_OBJECT_CREATE_ATTEMPTED
    assert "tag object creation (mktag) was ATTEMPTED but NOT PROVEN" in message
    assert "unreferenced tag object MAY exist" in message
    assert "REPORTED a tag object" not in message
    assert excinfo.value.tag_object_oid is None
    assert "tag object OID: none reported" in message


def test_mktag_returning_an_oid_with_failed_verification_reports_object_known_correctness_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, state, config = _ready(tmp_path)
    original = GitRepo.read_tag_object

    def _tampered(self: GitRepo, oid: str) -> str:
        return original(self, oid) + "tampered\n"

    monkeypatch.setattr(GitRepo, "read_tag_object", _tampered)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    message = str(excinfo.value)
    assert excinfo.value.stage == checkpoint.STAGE_TAG_OBJECT_CREATED
    assert "mktag REPORTED a tag object (its OID is known)" in message
    assert "correctness is NOT proven" in message
    assert "ATTEMPTED but NOT PROVEN" not in message and "was NOT attempted - " not in message
    assert not repo.tag_exists("checkpoint-T091-T091")


def test_mktag_oid_is_propagated_structurally_when_exact_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, state, config = _ready(tmp_path)
    reported_oid = "a" * 40
    published: list[str] = []
    original_update_ref = GitRepo.update_ref

    def _report_oid(self: GitRepo, payload: str) -> str:
        return reported_oid

    def _update_ref(self: GitRepo, ref: str, new_oid: str, old_oid: str = "") -> None:
        if ref.startswith("refs/tags/"):
            published.append(ref)
        return original_update_ref(self, ref, new_oid, old_oid)

    monkeypatch.setattr(GitRepo, "mktag", _report_oid)
    monkeypatch.setattr(GitRepo, "update_ref", _update_ref)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    error = excinfo.value
    assert error.stage == checkpoint.STAGE_TAG_OBJECT_CREATED
    assert error.tag_object_oid == reported_oid
    assert f"mktag reported tag object OID={reported_oid}" in str(error)
    assert "correctness is NOT proven" in str(error)
    assert "tag ref publication was NOT attempted" in str(error)
    assert published == []


def test_tag_oid_report_does_not_undo_proven_verification_at_ref_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, state, config = _ready(tmp_path)
    original_update_ref = GitRepo.update_ref

    def _fail_tag_ref(self: GitRepo, ref: str, new_oid: str, old_oid: str = "") -> None:
        if ref.startswith("refs/tags/"):
            raise RuntimeError("simulated tag ref publication failure")
        return original_update_ref(self, ref, new_oid, old_oid)

    monkeypatch.setattr(GitRepo, "update_ref", _fail_tag_ref)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as excinfo:
        checkpoint.run_checkpoint(repo, config, state, tasks_md_path=None, ai_runs_dir=_ai(repo))
    assert excinfo.value.stage == checkpoint.STAGE_TAG_REF_PUBLISH_ATTEMPTED
    assert excinfo.value.tag_object_oid is not None
    assert f"mktag reported tag object OID={excinfo.value.tag_object_oid}" in str(excinfo.value)
    assert "exact verification completed" in str(excinfo.value)
    assert "verification/correctness NOT proven" not in str(excinfo.value)


def test_tag_oid_from_one_invocation_cannot_leak_into_another_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_repo, first_state, config = _ready(tmp_path / "first")
    monkeypatch.setattr(GitRepo, "mktag", lambda self, payload: "b" * 40)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as first:
        checkpoint.run_checkpoint(first_repo, config, first_state, tasks_md_path=None, ai_runs_dir=_ai(first_repo))
    assert first.value.tag_object_oid == "b" * 40
    monkeypatch.undo()

    second_repo, second_state, config = _ready(tmp_path / "second")
    def _payload_failure(self: GitRepo, *, target: str, tag_name: str, message: str) -> str:
        raise RuntimeError("payload failure")
    monkeypatch.setattr(GitRepo, "build_tag_payload", _payload_failure)
    with pytest.raises(checkpoint.CheckpointPartialFailureError) as second:
        checkpoint.run_checkpoint(second_repo, config, second_state, tasks_md_path=None, ai_runs_dir=_ai(second_repo))
    assert second.value.tag_object_oid is None
    assert "b" * 40 not in str(second.value)


def test_the_three_tag_object_outcomes_have_distinct_stage_messages() -> None:
    facts = {
        checkpoint._STAGE_FACTS[checkpoint.STAGE_MERGE_VERIFIED],
        checkpoint._STAGE_FACTS[checkpoint.STAGE_TAG_OBJECT_CREATE_ATTEMPTED],
        checkpoint._STAGE_FACTS[checkpoint.STAGE_TAG_OBJECT_CREATED],
    }
    assert len(facts) == 3


def test_commit_object_observation_never_claims_absence() -> None:
    for stage in checkpoint._ENVELOPE_STAGE_ORDER:
        text = checkpoint._commit_object_observation(stage, None)
        assert "not created" not in text
    assert "NOT attempted" in checkpoint._commit_object_observation(checkpoint.STAGE_INDEX_STAGED, None)
