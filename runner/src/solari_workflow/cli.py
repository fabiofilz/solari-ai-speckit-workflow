"""Argparse-based CLI foundation for `solari-workflow` (contracts/cli-interface.md).

Every subcommand is issued by the Orchestrator, never directly by the
User and never by Claude or Codex (research.md §0.0). There is no "run
everything" command — the absence of such a command is the technical
enforcement of Controlled Automation (constitution Principle VII).

This module declares the full v1 subcommand surface so `--help` and
argument parsing work end-to-end today; only `readiness-check` has a real
body in this Foundational phase (config loading + the readiness engine
both already exist). Every other subcommand's body is a documented
`NotImplementedError` placeholder until the phase that implements it
(`init` → User Story 1; `start-block`/`run-claude`/`run-codex-gate`/
`checkpoint`/`resume` → User Story 2; `retention` → User Story 5;
`status` → User Story 2) — the CLI boundary still applies the uniform
exit-code convention and stderr error formatting to those placeholders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from solari_workflow.actors import claude as claude_actor
from solari_workflow.actors import codex as codex_actor
from solari_workflow.config import init as config_init
from solari_workflow.config.loader import load_config
from solari_workflow.config.schema import WorkflowConfig
from solari_workflow.errors import (
    EXIT_MANUAL_INTERVENTION,
    EXIT_NEGATIVE_RESULT,
    EXIT_SUCCESS,
    GitAmbiguityError,
    SpecKitFeatureAmbiguityError,
    SpecKitFeatureIdentityError,
    TaskRangeError,
    UnsupportedCLICapabilityError,
    WorkflowError,
)
from solari_workflow.fingerprint.engine import Fingerprint, compute_fingerprint, verify_fingerprint
from solari_workflow.git import branch as git_branch
from solari_workflow.git import candidate_tree as candidate_tree_mod
from solari_workflow.git import checkpoint as checkpoint_mod
from solari_workflow.git import ops as git_ops
from solari_workflow.lock import run_lock as run_lock_mod
from solari_workflow.platform import proc
from solari_workflow.readiness.engine import run_readiness_gate
from solari_workflow.runs import audit as audit_mod
from solari_workflow.speckit import integration as speckit_integration
from solari_workflow.state import block_state as block_state_mod
from solari_workflow.state import gate_identity as gate_identity_mod

ProgArgs = argparse.Namespace


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Test-only escape hatches (never a documented `solari-workflow` flag, and
# never consulted anywhere except immediately before invoking `claude`/
# `codex`): a JSON-encoded argv list letting the integration test suite
# point `run-claude`/`run-codex-gate` at `tests/fixtures/fake_claude.py`/
# `fake_codex.py` (research.md §18: "Tests point the runner at these via
# an executable-path override") without a real, paid model call and
# without adding a production-facing CLI surface for it.
_CLAUDE_ARGV_PREFIX_ENV = "SOLARI_WORKFLOW_TEST_CLAUDE_ARGV_PREFIX"
_CODEX_ARGV_PREFIX_ENV = "SOLARI_WORKFLOW_TEST_CODEX_ARGV_PREFIX"


def _test_argv_prefix(env_var: str) -> list[str] | None:
    raw = os.environ.get(env_var)
    if raw is None:
        return None
    return list(json.loads(raw))


def _block_scope(state: block_state_mod.BlockState) -> str:
    return f"{state.first_task}-{state.last_task}: {state.block_name}"


def _canonicalize_under_root(project_root: Path, candidate: Path) -> str:
    """Resolve `candidate` (symlinks included) and return it as a POSIX
    path relative to `project_root` (item 7: "canonicalized relative to
    the project root, not current working directory"; "persisted feature
    identity cannot escape project root via traversal"; "symlink
    resolution must not redirect outside the project root").

    `Path.resolve()` follows symlinks and normalizes `..`/`.` components
    for BOTH sides before comparison, so a symlink pointing outside
    `project_root`, or a path that only reaches outside it via `..`
    traversal, is caught identically - `relative_to` raises `ValueError`
    for anything not actually inside the resolved root, converted here
    into a fail-closed :class:`SpecKitFeatureIdentityError`.
    """
    resolved_root = project_root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        relative = resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise SpecKitFeatureIdentityError(
            f"resolved tasks.md path {resolved_candidate} escapes the project root "
            f"{resolved_root} (symlink or path traversal) - refusing to persist an "
            "out-of-root feature identity"
        ) from exc
    return relative.as_posix()


def _resolve_tasks_md_path(project_root: Path, config: WorkflowConfig) -> str | None:
    """Resolve the Spec Kit `tasks.md` this block's task IDs/titles are
    bound to — UNAMBIGUOUSLY or not at all (A5/item-7 remediation) —
    returning a canonical, project-root-relative POSIX path string (never
    an absolute, machine-specific path — item 7: "prefer canonical
    project-root-relative identity plus project-root anchoring").

    `speckit/integration.py` (T013) deliberately does not decide which
    Spec Kit feature directory is "current" - that decision is left to
    its caller, by its own docstring. No field anywhere in the current
    config schema/data model/contracts records an authoritative "active
    feature" identity (`[speckit].spec_dir` is documented as the ROOT of
    Spec Kit artifacts, not a specific feature). Absent that authority,
    this function resolves only the cases that are genuinely unambiguous
    and FAILS CLOSED — raising, never guessing — on any real ambiguity:

    - `tasks.md` directly at `spec_dir`'s root, AND NO child feature
      directory also has one: unambiguous, used as-is.
    - Exactly ONE subdirectory of `spec_dir` containing its own
      `tasks.md`, AND NO root-level `tasks.md` also exists: unambiguous
      (the common one-active-feature-at-a-time case, matching this very
      repository's own `specs/001-workflow-v1/` layout), used as-is.
    - Zero candidates found anywhere: a legitimate absence, not an
      ambiguity - returns `None` (callers degrade gracefully: task titles
      become unavailable, which is cosmetic tag/gate-context metadata,
      never a reason to block a Candidate Tree/Fingerprint/checkpoint
      safety guarantee).
    - A root-level `tasks.md` COEXISTING with one or more child feature
      directories that also have their own, OR two-or-more child feature
      directories: a real ambiguity (item 7: "root tasks.md + child
      feature tasks.md is ambiguous unless the formal project layout
      explicitly designates one") — no config field designates one, so
      this always raises :class:`~solari_workflow.errors.
      SpecKitFeatureAmbiguityError` rather than guessing (e.g. the prior,
      REMOVED "pick the highest-numbered directory" heuristic, or an
      implicit "root wins" rule this codebase never actually adopted).

    The resolved value (or `None`) is meant to be resolved exactly ONCE,
    at `start-block` time, and persisted into `BlockState.tasks_md_path`
    (A5's own "persist that identity" requirement) — `run-codex-gate`/
    `checkpoint` read and re-validate the persisted value
    (`_resolve_persisted_tasks_md_path`) rather than calling this
    function again, so they are immune to the repository's Spec Kit
    layout changing mid-block.
    """
    spec_root = project_root / config.speckit.spec_dir
    direct = spec_root / "tasks.md"
    has_direct = direct.is_file()

    child_candidates: list[Path] = []
    if spec_root.is_dir():
        child_candidates = sorted(
            child for child in spec_root.iterdir() if child.is_dir() and (child / "tasks.md").is_file()
        )

    total_candidates = (1 if has_direct else 0) + len(child_candidates)
    if total_candidates > 1:
        names = [str(direct.relative_to(project_root))] if has_direct else []
        names += [str((c / "tasks.md").relative_to(project_root)) for c in child_candidates]
        raise SpecKitFeatureAmbiguityError(
            "cannot determine which Spec Kit feature this block's task IDs belong to: "
            f"{total_candidates} candidate tasks.md files exist under {config.speckit.spec_dir!r} "
            f"({', '.join(names)}), and no authoritative feature-identity field exists in the "
            "current config/state/contracts to disambiguate. Resolve manually (e.g. move or "
            "consolidate the Spec Kit feature directories) before starting this block."
        )

    if has_direct:
        return _canonicalize_under_root(project_root, direct)
    if child_candidates:
        return _canonicalize_under_root(project_root, child_candidates[0] / "tasks.md")
    return None


def _resolve_persisted_tasks_md_path(project_root: Path, tasks_md_relative: str | None) -> Path | None:
    """Re-resolve a PERSISTED, canonical `BlockState.tasks_md_path` back
    into a concrete filesystem path, re-validating it at read time
    (item 7: "later commands resolve against the stored project-root-
    relative canonical identity"; "selected file must still exist and be
    the same intended source"; "moving invocation CWD must not alter
    resolution").

    Always resolves against `project_root` (an explicit CLI argument,
    never the process's current working directory), so a later command
    invoked from a different CWD still resolves identically. Fails closed
    (:class:`SpecKitFeatureIdentityError`) if the stored relative path
    would resolve outside the project root (a corrupted/hand-edited
    record) or if the file it names no longer exists - a stale or
    missing selected tasks file is an integrity violation to report, not
    a legitimate absence to silently degrade from (unlike a `None`
    `tasks_md_path`, which IS a legitimate, no-op absence - see
    :func:`_resolve_tasks_md_path`).
    """
    if tasks_md_relative is None:
        return None
    resolved_root = project_root.resolve()
    candidate = (project_root / tasks_md_relative).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise SpecKitFeatureIdentityError(
            f"persisted feature identity {tasks_md_relative!r} resolves outside the project "
            f"root {resolved_root} - refusing to use it"
        ) from exc
    if not candidate.is_file():
        raise SpecKitFeatureIdentityError(
            f"persisted feature identity {tasks_md_relative!r} no longer exists at "
            f"{candidate} - the selected tasks.md was moved or deleted since this block started"
        )
    return project_root / tasks_md_relative


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solari-workflow", description="AI Spec Kit Workflow v1 Runner"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Scaffold .ai-workflow.toml for a project that doesn't have one yet"
    )
    init_parser.add_argument("--project-name", required=True)
    init_parser.add_argument("--stack", required=True, choices=["python", "node", "go", "rust", "other"])
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config after showing a diff")
    init_parser.add_argument(
        "--project-root", default=".", help="Path to the project root (default: current directory)"
    )
    init_parser.add_argument("--project-type", default="service")
    init_parser.add_argument("--main-branch", default="main")
    init_parser.add_argument("--spec-dir", default="specs")
    init_parser.add_argument(
        "--runtime-version",
        default=None,
        help="Overrides the generic per-stack default (never guessed if omitted)",
    )
    init_parser.add_argument("--dependency-manager", default=None)
    init_parser.add_argument("--manifest", default=None)
    init_parser.add_argument("--lockfile", default=None)
    init_parser.set_defaults(handler=_cmd_init)

    readiness_parser = subparsers.add_parser(
        "readiness-check", help="Run the Project Readiness Gate against the current working tree"
    )
    readiness_parser.add_argument(
        "--project-root", default=".", help="Path to the project root (default: current directory)"
    )
    readiness_parser.set_defaults(handler=_cmd_readiness_check)

    start_block_parser = subparsers.add_parser(
        "start-block", help="Create the branch for one Orchestrator-defined development block"
    )
    start_block_parser.add_argument("--first-task", required=True)
    start_block_parser.add_argument("--last-task", required=True)
    start_block_parser.add_argument("--block-name", required=True)
    start_block_parser.add_argument(
        "--project-root", default=".", help="Path to the project root (default: current directory)"
    )
    start_block_parser.set_defaults(handler=_cmd_start_block)

    run_claude_parser = subparsers.add_parser(
        "run-claude", help="Invoke Claude for one implementation/remediation execution"
    )
    run_claude_parser.add_argument("--model", required=True)
    run_claude_parser.add_argument("--effort", required=True)
    run_claude_parser.add_argument("--session-mode", required=True, choices=["NEW", "CONTINUE"])
    run_claude_parser.add_argument("--session-id", default=None)
    run_claude_parser.add_argument("--purpose", required=True)
    run_claude_parser.add_argument(
        "--project-root", default=".", help="Path to the project root (default: current directory)"
    )
    run_claude_parser.set_defaults(handler=_cmd_run_claude)

    run_codex_gate_parser = subparsers.add_parser(
        "run-codex-gate", help="Invoke Codex for one independent, read-only gate"
    )
    run_codex_gate_parser.add_argument(
        "--project-root", default=".", help="Path to the project root (default: current directory)"
    )
    run_codex_gate_parser.set_defaults(handler=_cmd_run_codex_gate)

    checkpoint_parser = subparsers.add_parser(
        "checkpoint", help="Stage, commit, merge, tag, and retain the current block"
    )
    checkpoint_parser.add_argument(
        "--project-root", default=".", help="Path to the project root (default: current directory)"
    )
    checkpoint_parser.set_defaults(handler=_cmd_checkpoint)

    resume_parser = subparsers.add_parser(
        "resume", help="Revalidate and continue an interrupted block from its last safe stage"
    )
    resume_parser.add_argument(
        "--project-root", default=".", help="Path to the project root (default: current directory)"
    )
    resume_parser.set_defaults(handler=_cmd_resume)

    retention_parser = subparsers.add_parser(
        "retention", help="Run branch retention standalone"
    )
    retention_parser.add_argument("--dry-run", action="store_true")
    retention_parser.set_defaults(handler=_cmd_not_implemented)

    status_parser = subparsers.add_parser(
        "status", help="Read-only report of lock ownership and the active block's state"
    )
    status_parser.add_argument(
        "--project-root", default=".", help="Path to the project root (default: current directory)"
    )
    status_parser.set_defaults(handler=_cmd_status)

    return parser


def _cmd_not_implemented(args: ProgArgs) -> int:
    raise NotImplementedError(f"'{args.command}' is implemented in a later phase of this feature")


def _cmd_init(args: ProgArgs) -> int:
    """Scaffold `.ai-workflow.toml` (User Story 1, T023, `contracts/cli-
    interface.md`'s `init` subcommand).

    Exit codes: `0` success; `1` a config already exists and `--force` was
    not given (a substantive negative result, not an exception — matches
    `readiness-check`'s own pattern of returning `EXIT_NEGATIVE_RESULT` as
    data rather than raising). Every other failure (an unwritable project
    root, a template that somehow fails its own self-validation) is a
    genuine :class:`~solari_workflow.errors.WorkflowError`/unexpected
    exception and is left to `main`'s own boundary handling (exit `2`).

    **Failure-atomic across its two files** (T022-T032 re-gate, Finding 5
    — MINOR): both the new config content and the new `.gitignore`
    content are fully computed before either file on disk is touched;
    both writes go through `config/init.py`'s `atomic_write_text` (temp
    file + `os.replace`, never a partial write); and if the `.gitignore`
    write fails AFTER the config was already (re)written, the config is
    rolled back to its exact prior state (removed if it did not exist
    before, restored verbatim if it did) before the failure propagates —
    `init` never leaves a newly created/replaced config committed to disk
    while `.gitignore` failed to update.
    """
    project_root = Path(args.project_root)
    options = config_init.InitOptions(
        project_name=args.project_name,
        stack=args.stack,
        project_type=args.project_type,
        main_branch=args.main_branch,
        spec_dir=args.spec_dir,
        runtime_version=args.runtime_version,
        dependency_manager=args.dependency_manager,
        manifest=args.manifest,
        lockfile=args.lockfile,
    )
    new_config_content, config = config_init.build_config(options)
    config_path = project_root / config_init.CONFIG_FILENAME
    gitignore_path = project_root / config_init.GITIGNORE_FILENAME

    if config_path.is_file():
        if not args.force:
            print(
                f"error: {config_init.CONFIG_FILENAME} already exists at {config_path}; "
                "use --force to overwrite (a diff will be shown first).",
                file=sys.stderr,
            )
            return EXIT_NEGATIVE_RESULT
        existing_content = config_path.read_text(encoding="utf-8")
        diff = config_init.render_diff(existing_content, new_config_content)
        print(diff if diff else "(no textual differences)")

    project_root.mkdir(parents=True, exist_ok=True)

    # Snapshot prior state (for rollback) and prepare BOTH files' final
    # content before either is written to disk.
    original_config_content = config_path.read_text(encoding="utf-8") if config_path.is_file() else None
    original_gitignore_content = (
        gitignore_path.read_text(encoding="utf-8") if gitignore_path.is_file() else None
    )
    new_gitignore_content = config_init.compute_gitignore_content(
        original_gitignore_content, config.ai_runs.directory
    )

    config_init.atomic_write_text(config_path, new_config_content)
    try:
        config_init.atomic_write_text(gitignore_path, new_gitignore_content)
    except OSError as exc:
        config_init.restore_or_remove(config_path, original_config_content)
        raise WorkflowError(
            f"could not update {config_init.GITIGNORE_FILENAME} at {gitignore_path}: {exc}; "
            f"{config_init.CONFIG_FILENAME} was rolled back to its prior state"
        ) from exc

    print(f"wrote {config_path}")
    return EXIT_SUCCESS


def _cmd_readiness_check(args: ProgArgs) -> int:
    """Read-only: makes no Git or filesystem changes, never touches block-state.

    Consumes `run_readiness_gate`'s own fail-closed `overall_status`
    directly rather than re-deriving completeness policy here — the
    engine, not this subcommand, is the single source of truth for
    whether a stack's readiness result is complete (see
    `readiness/engine.py`'s `ReadinessResult.overall_status` docstring).
    `"INCOMPLETE"` (stack-specific checks not registered for this
    project's stack — Phase 6, T054-T063, not implemented in this build)
    maps to `UnsupportedCLICapabilityError` (exit `2`, manual
    intervention — not a `0` success and not a `1` negative result, since
    neither is true); the common-check results that DID run are always
    printed in full first, regardless of the outcome. `"other"` is
    unaffected: it never needs stack-specific checks, by design, so its
    engine result is always a real `PASS`/`FAIL`.
    """
    project_root = Path(args.project_root)
    config, warnings = load_config(project_root / ".ai-workflow.toml")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    result = run_readiness_gate(config, project_root)
    for check in result.checks:
        print(f"[{check.status}] {check.name}: {check.message}")

    if result.overall_status == "INCOMPLETE":
        raise UnsupportedCLICapabilityError(
            "readiness-check cannot yet report a complete result for stack "
            f"'{config.environment.stack}': stack-specific readiness checks "
            "are not implemented in this build (planned for a later phase). "
            "The common checks above ran and are accurate, but no overall "
            "PASS/FAIL can be claimed until the stack-specific check "
            "registry exists for this stack."
        )

    return EXIT_SUCCESS if result.overall_status == "PASS" else EXIT_NEGATIVE_RESULT


def _refuse_on_unresolved_mutation_boundary(state: block_state_mod.BlockState | None, command: str) -> None:
    """Fifth remediation (B2): an unresolved checkpoint mutation boundary is
    an independent safety interlock with HIGHER precedence than the mutable
    lifecycle `state` field. Every workflow-progressing command calls this
    first, so no value of `state` (RUNNING, READY_TO_RESUME, COMPLETED, ...)
    can let a command proceed, overwrite the record, or clear the marker.
    Nothing in the Runner clears it; manual recovery is outside T033-T050."""
    if state is not None and state.checkpoint_mutation_boundary is not None:
        raise GitAmbiguityError(
            f"{command} refused: an unresolved checkpoint mutation boundary "
            f"({state.checkpoint_mutation_boundary!r}) is recorded in block-state (state={state.state!r}); "
            "it takes precedence over the lifecycle state - manual recovery of the repository is required "
            "before any workflow-progressing command may run (state left unchanged)"
        )


def _cmd_start_block(args: ProgArgs) -> int:
    """`start-block` (T037, `contracts/cli-interface.md`): readiness gate
    -> real staging-area precondition -> branch-collision check ->
    deterministically-derived branch created + checked out from `main` ->
    initial block-state (`state: RUNNING`, `last_safe_stage:
    BRANCH_CREATED`).

    Task-range ordering (`--first-task <= --last-task`) is validated HERE,
    not inside `fingerprint/engine.py`'s `Fingerprint` construction, which
    intentionally validates only task-ID *shape* - the lifecycle layer
    that owns an Orchestrator-supplied range is responsible for its
    semantic ordering.
    """
    project_root = Path(args.project_root)
    config, warnings = load_config(project_root / ".ai-workflow.toml")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    # B3 (fourth remediation): the block identity is validated where it is
    # first accepted, before anything is persisted or rendered.
    git_branch.validate_task_id(args.first_task)
    git_branch.validate_task_id(args.last_task)
    git_branch.validate_block_name(args.block_name)

    try:
        first_n = int(git_branch.task_id_number(args.first_task))
        last_n = int(git_branch.task_id_number(args.last_task))
    except ValueError as exc:
        raise TaskRangeError(
            "--first-task/--last-task must be numeric task IDs (e.g. 'T091'); got "
            f"--first-task={args.first_task!r} --last-task={args.last_task!r}"
        ) from exc
    if first_n > last_n:
        raise TaskRangeError(
            f"--first-task ({args.first_task}) must be <= --last-task ({args.last_task}); "
            "the task range is not semantically ordered"
        )

    # B2 (fifth remediation): the prior block record is inspected before
    # readiness, Git, or any write. An unresolved checkpoint mutation
    # boundary is checked FIRST and wins over any lifecycle state.
    ai_runs_dir = project_root / config.ai_runs.directory
    existing_state = block_state_mod.load_block_state(ai_runs_dir)
    _refuse_on_unresolved_mutation_boundary(existing_state, "start-block")
    # M3: start-block must not silently replace an unfinished active
    # block. `COMPLETED` is the only state a prior block may be in for a
    # fresh `start-block` to proceed - anything else (including a
    # lossy-regenerated `BLOCKED_MANUAL` record) needs an explicit
    # `resume`/manual resolution first, never a silent overwrite of its
    # tracking.
    if existing_state is not None and existing_state.state != "COMPLETED":
        raise GitAmbiguityError(
            "start-block refused: an unfinished block is already active "
            f"(branch={existing_state.branch_name!r}, state={existing_state.state!r}); "
            "resolve or resume it before starting a new one"
        )

    result = run_readiness_gate(config, project_root)
    for check in result.checks:
        print(f"[{check.status}] {check.name}: {check.message}")
    if result.overall_status == "FAIL":
        return EXIT_NEGATIVE_RESULT
    if result.overall_status == "INCOMPLETE":
        raise UnsupportedCLICapabilityError(
            "start-block cannot proceed: readiness is INCOMPLETE for stack "
            f"'{config.environment.stack}' (stack-specific checks not implemented in this build)"
        )

    repo = git_ops.open_repository(project_root)
    repo.check_staging_precondition()

    # B2: re-read immediately before any write, so a boundary recorded
    # while readiness ran is still honored (never overwritten).
    existing_state = block_state_mod.load_block_state(ai_runs_dir)
    _refuse_on_unresolved_mutation_boundary(existing_state, "start-block")
    if existing_state is not None and existing_state.state != "COMPLETED":
        raise GitAmbiguityError(
            "start-block refused: an unfinished block is already active "
            f"(branch={existing_state.branch_name!r}, state={existing_state.state!r}); "
            "resolve or resume it before starting a new one"
        )

    # A5: resolve this block's exact Spec Kit feature identity ONCE, here,
    # failing closed on ambiguity - never re-resolved by a later
    # run-codex-gate/checkpoint call (see `_resolve_tasks_md_path`'s own
    # docstring).
    tasks_md_path = _resolve_tasks_md_path(project_root, config)

    branch_name = git_branch.build_branch_name(args.first_task, args.block_name)
    if repo.branch_exists(branch_name):
        raise GitAmbiguityError(f"start-block refused: branch {branch_name!r} already exists unexpectedly")

    # B2 (fourth remediation): the block's base is the configured main
    # BRANCH, resolved only through refs/heads/<main> - never a short name a
    # same-named tag could win.
    main_base_oid = repo.branch_oid(config.project.main_branch)
    repo.create_branch(branch_name, start_point=main_base_oid)
    repo.switch(branch_name)
    if repo.current_branch() != branch_name or repo.rev_parse("HEAD") != main_base_oid:
        raise GitAmbiguityError(
            f"start-block: HEAD is not branch {branch_name!r} at main's branch commit {main_base_oid!r}"
        )
    # Item 6: `HEAD` immediately after branch creation - Claude never
    # commits (research.md §0.9), so this stays the expected `HEAD`
    # through BRANCH_CREATED/IMPLEMENTATION_COMPLETE; `resume` verifies
    # against it before proceeding from ANY stage.
    expected_head_oid = repo.rev_parse("HEAD")

    new_state = block_state_mod.new_block_state(
        branch_name=branch_name,
        first_task=args.first_task,
        last_task=args.last_task,
        block_name=args.block_name,
        tasks_md_path=tasks_md_path,
        expected_head_oid=expected_head_oid,
    )
    block_state_mod.write_block_state(ai_runs_dir, new_state)

    print(f"created and checked out branch {branch_name!r}")
    return EXIT_SUCCESS


# Item 10: `run-claude` may only legitimately START from these `state`
# values (checked independently of `last_safe_stage`, "central enough to
# avoid divergent rules"). `BLOCKED_MANUAL` is added dynamically ONLY for
# a `resume`-delegated call (`args._via_resume`) - a direct Orchestrator
# call while `BLOCKED_MANUAL` must go through `resume` instead, which
# revalidates before replaying the authorized invocation (item 6).
_RUN_CLAUDE_DIRECT_ELIGIBLE_STATES = (
    "RUNNING",
    "QA_REMEDIATION_REQUIRED",
    "ARCHITECTURAL_DECISION_REQUIRED",
    "READY_TO_RESUME",
)


def _cmd_run_claude(args: ProgArgs) -> int:
    """`run-claude` (T038): lifecycle-state guard (item 10) -> verifies
    the current branch against the active block-state (refuses rather
    than creates one) -> persists the Orchestrator-approved invocation
    (item 6, BEFORE invoking the actor, so a crash mid-session still
    leaves it recoverable) -> invokes `claude` -> updates block-state per
    the parsed `STATUS:` outcome — never touches Git itself (research.md
    §0.11 is enforced entirely inside `actors/claude.py`'s own invocation
    setup, T048).

    **Prompt delivery**: `--purpose` is both the audit-trail label AND the
    literal instructions handed to Claude - see `actors/claude.py`'s own
    module docstring for why, given `contracts/cli-interface.md` defines
    no separate instructions channel for this subcommand.
    """
    project_root = Path(args.project_root)
    config, warnings = load_config(project_root / ".ai-workflow.toml")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    ai_runs_dir = project_root / config.ai_runs.directory
    state = block_state_mod.load_block_state(ai_runs_dir)
    if state is None:
        raise WorkflowError("no active block; run 'start-block' first")
    _refuse_on_unresolved_mutation_boundary(state, "run-claude")

    # Item 10: lifecycle-state guard.
    via_resume = bool(getattr(args, "_via_resume", False))
    eligible_states = _RUN_CLAUDE_DIRECT_ELIGIBLE_STATES + (("BLOCKED_MANUAL",) if via_resume else ())
    if state.state not in eligible_states:
        if state.state == "BLOCKED_MANUAL":
            raise GitAmbiguityError(
                "run-claude refused: block-state is BLOCKED_MANUAL; issue 'resume' (after "
                "resolving whatever caused the stop) rather than calling run-claude directly"
            )
        raise GitAmbiguityError(
            f"run-claude refused: block-state is {state.state!r}, which is not a valid "
            f"starting state for implementation (eligible: {', '.join(eligible_states)})"
        )
    if state.checkpoint_mutation_boundary is not None:
        raise GitAmbiguityError(
            "run-claude refused: a previous checkpoint attempt failed after mutating Git state "
            f"(boundary {state.checkpoint_mutation_boundary!r}); manual recovery is required"
        )
    if state.last_safe_stage == "GATE_PASSED":
        raise GitAmbiguityError(
            "run-claude refused: this block already has an eligible gate at GATE_PASSED; "
            "formal remediation semantics require a fresh, superseding run-codex-gate outcome "
            "to invalidate it before further implementation - checkpoint the current state, or "
            "obtain a new gate result, before running run-claude again"
        )

    repo = git_ops.open_repository(project_root)
    current = repo.current_branch()
    if current != state.branch_name:
        raise GitAmbiguityError(
            f"run-claude refused: current branch {current!r} does not match the active "
            f"block's branch {state.branch_name!r}"
        )

    # Item 6: persist the Orchestrator-approved invocation BEFORE
    # attempting it, so a crash mid-session (not merely a parsed
    # BLOCKED/operational-failure outcome, which also updates this same
    # field below) still leaves `resume` something authorized to replay.
    pending = block_state_mod.PendingClaudeInvocation(
        model=args.model,
        effort=args.effort,
        session_mode=args.session_mode,
        session_id=args.session_id,
        purpose=args.purpose,
    )
    block_state_mod.write_block_state(
        ai_runs_dir, replace(state, pending_claude_invocation=pending, updated_at=_utc_now_iso())
    )

    header_kwargs = dict(
        tool="claude",
        model=args.model,
        effort=args.effort,
        session_mode=args.session_mode,
        prior_session_id=args.session_id,
        speckit_involved=True,
        scope=_block_scope(state),
        purpose=args.purpose,
        workflow_schema_version=config.workflow.schema_version,
        workflow_version=config.workflow.version,
    )
    prompt_content = audit_mod.render_metadata_header(**header_kwargs) + "\n" + args.purpose + "\n"
    record = audit_mod.create_run_record(
        ai_runs_dir, slug_source=f"{state.branch_name}-run-claude-{args.purpose}", prompt_content=prompt_content
    )

    try:
        outcome = claude_actor.run_claude_session(
            model=args.model,
            effort=args.effort,
            session_mode=args.session_mode,
            session_id=args.session_id,
            prompt_body=args.purpose,
            cwd=str(project_root),
            argv_prefix=_test_argv_prefix(_CLAUDE_ARGV_PREFIX_ENV),
        )
    except proc.TransientOperationalError as exc:
        # The pending invocation stays recorded - this IS the operational
        # failure `resume` needs to replay (item 6).
        updated = replace(
            state,
            state="BLOCKED_MANUAL",
            retry_count_current_stage=1,
            pending_claude_invocation=pending,
            updated_at=_utc_now_iso(),
        )
        block_state_mod.write_block_state(ai_runs_dir, updated)
        audit_mod.write_result(
            record,
            audit_mod.render_metadata_header(**header_kwargs, paired_prompt_stem=record.stem)
            + f"\n- Outcome: BLOCKED_MANUAL (operational failure after single retry)\n\n{exc}\n",
        )
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MANUAL_INTERVENTION

    audit_mod.write_result(
        record,
        audit_mod.render_metadata_header(**header_kwargs, paired_prompt_stem=record.stem)
        + f"\n- Status: {outcome.status}\n\n"
        + outcome.transcript,
    )

    if outcome.status == "COMPLETE":
        # Implementation is done - nothing left to replay.
        updated = replace(
            state,
            state="RUNNING",
            last_safe_stage="IMPLEMENTATION_COMPLETE",
            retry_count_current_stage=0,
            pending_claude_invocation=None,
            updated_at=_utc_now_iso(),
        )
        block_state_mod.write_block_state(ai_runs_dir, updated)
        return EXIT_SUCCESS
    if outcome.status == "ARCHITECTURAL_DECISION_REQUIRED":
        # A fresh Orchestrator decision (and likely different --purpose)
        # is required next, never a blind replay of THIS invocation.
        updated = replace(
            state, state="ARCHITECTURAL_DECISION_REQUIRED", pending_claude_invocation=None, updated_at=_utc_now_iso()
        )
        block_state_mod.write_block_state(ai_runs_dir, updated)
        return EXIT_NEGATIVE_RESULT
    # STATUS: BLOCKED - the pending invocation stays recorded: per
    # research.md's own resume flow, the User authorizes `resume` only
    # after resolving whatever caused the stop, so replaying the SAME
    # authorized instructions against the now-resolved situation is the
    # intended recovery (item 6).
    updated = replace(state, state="BLOCKED_MANUAL", pending_claude_invocation=pending, updated_at=_utc_now_iso())
    block_state_mod.write_block_state(ai_runs_dir, updated)
    return EXIT_MANUAL_INTERVENTION


def _describe_binary_evidence(repo: git_ops.GitRepo, fingerprint_x: Fingerprint) -> str:
    """Item 5: deterministic, integrity-bound identity metadata for every
    binary path `diff_between` can only report as "Binary files ...
    differ" - never arbitrary binary content dumped into the prompt.

    For each path `git diff --numstat` marks binary, resolves its exact
    blob identity WITHIN Candidate Tree X (`ls_tree_entry` against
    `candidate_tree_oid` - never the live filesystem), plus its mode and
    byte size (`blob_size`), and states explicitly that the file is
    binary and that this identity IS part of X. A path deleted relative
    to X (present at `head_oid`, absent at `candidate_tree_oid`) is
    reported as a deletion instead of a phantom "missing" entry.
    """
    # Third remediation (M7): `diff_numstat` is rename-free and
    # NUL-delimited, so every `path` below is a literal Candidate-Tree (or
    # HEAD-only, for deletions) path - a rename is always an old-path
    # deletion plus a new-path addition, never a display string.
    numstat = repo.diff_numstat(fingerprint_x.head_oid, fingerprint_x.candidate_tree_oid)
    binary_paths = [path for added, deleted, path in numstat if added is None and deleted is None]
    if not binary_paths:
        return ""

    candidate_entries = repo.ls_tree_entries(fingerprint_x.candidate_tree_oid)
    lines = ["", "Binary files in Candidate Tree X (content omitted; identity below IS part of X):"]
    for path in binary_paths:
        entry = candidate_entries.get(path)
        if entry is None:
            lines.append(f"  - {path}: binary file DELETED relative to HEAD (absent from Candidate Tree X)")
            continue
        mode, obj_type, oid = entry
        # Fail closed: a Candidate-Tree binary path whose size cannot be
        # read would be incompletely bound evidence.
        size_text = f"{repo.blob_size(oid)} bytes"
        lines.append(
            f"  - {path}: binary {obj_type}, mode {mode}, candidate-tree blob oid {oid}, {size_text} "
            "- content omitted, this exact blob is what Candidate Tree X contains at this path"
        )
    return "\n".join(lines) + "\n"


def _build_codex_review_prompt(
    state: block_state_mod.BlockState,
    repo: git_ops.GitRepo,
    tasks_md_path: Path | None,
    fingerprint_x: Fingerprint,
) -> str:
    """Entirely Runner-authored context (research.md §0.12) - `run-codex-
    gate`'s own CLI synopsis takes no orchestrator-supplied free text at
    all, unlike `run-claude`'s `--purpose`.

    A2 remediation: `fingerprint_x` MUST already be computed from a
    Candidate Tree built before this function is ever called - the diff
    evidence below is derived by diffing `HEAD` directly against
    `fingerprint_x.candidate_tree_oid` (`GitRepo.diff_between`, a
    tree-object comparison), never the live working directory, so the
    evidence Codex reviews is provably the exact state Fingerprint X
    binds to (tracked changes, deletions, new files WITH content, mode
    changes, symlinks, and explicit binary-file acknowledgment - see
    `diff_between`'s own docstring). Item 5 supplements the plain-text
    diff with deterministic, integrity-bound binary-file identity
    metadata (`_describe_binary_evidence`) so a binary change is never
    silently invisible. The prompt also states Fingerprint X's own
    identity in full, so the resulting structured gate record (built by
    the caller from this same `fingerprint_x`) is never merely implied by
    which files happened to change.
    """
    titles = (
        speckit_integration.lookup_task_titles(tasks_md_path, state.first_task, state.last_task)
        if tasks_md_path is not None
        else {}
    )
    title_lines = (
        "\n".join(
            f"  - {task_id}: {title if title is not None else '(title not found in tasks.md)'}"
            for task_id, title in titles.items()
        )
        or "  (no tasks.md entries found for this range - feature identity unresolved or task IDs absent)"
    )
    diff_text = repo.diff_between(fingerprint_x.head_oid, fingerprint_x.candidate_tree_oid).strip() or (
        "(no differences between HEAD and the Candidate Tree)"
    )
    binary_evidence = _describe_binary_evidence(repo, fingerprint_x)
    return (
        f"Block: {state.block_name}\n"
        f"Branch: {state.branch_name}\n"
        f"Task range: {state.first_task}-{state.last_task}\n"
        f"Tasks:\n{title_lines}\n\n"
        "You are reviewing Candidate Tree X, an exact, content-addressed "
        "snapshot of this block's proposed state, identified by:\n"
        f"  head_oid: {fingerprint_x.head_oid}\n"
        f"  candidate_tree_oid: {fingerprint_x.candidate_tree_oid}\n\n"
        "The following diff is between HEAD and Candidate Tree X's own tree "
        "object directly (not the live working directory) - it fully "
        "represents X, including new files with their content:\n"
        f"{diff_text}\n"
        f"{binary_evidence}"
    )


# M3: the `last_safe_stage` values from which `run-codex-gate` may
# legitimately run - Claude must have already reached at least
# IMPLEMENTATION_COMPLETE. `GATE_PASSED` is allowed (a deliberate re-gate),
# and the new attempt immediately supersedes that earlier PASS.
_GATE_ELIGIBLE_STAGES = ("IMPLEMENTATION_COMPLETE", "CANDIDATE_TREE_BUILT", "GATE_PASSED")

# Third remediation (B2): the ONLY lifecycle states a new gate may start
# from. `BLOCKED_MANUAL` is never accepted directly (in particular after a
# partial checkpoint failure, it must not be normalized back into the
# ordinary lifecycle by a fresh gate); `resume` flips a revalidated block
# to `READY_TO_RESUME` before delegating here. `ARCHITECTURAL_DECISION_
# REQUIRED`, `RETRYABLE_ERROR` and `COMPLETED` are refused too.
_RUN_CODEX_GATE_ELIGIBLE_STATES = ("RUNNING", "QA_REMEDIATION_REQUIRED", "READY_TO_RESUME")


def _revoke_prior_gate(state: block_state_mod.BlockState) -> block_state_mod.BlockState:
    """Supersede every piece of gate-bound data together: `last_gate`, the
    cached feature/main identities, and the reference to the immutable
    gate-identity evidence; `last_safe_stage` drops from `GATE_PASSED` back
    to `IMPLEMENTATION_COMPLETE` (Claude's completed work is not undone,
    only the gate's verdict about it). Checkpoint eligibility can then be
    restored ONLY by a newly completed eligible PASS."""
    return replace(
        state,
        last_safe_stage="IMPLEMENTATION_COMPLETE" if state.last_safe_stage == "GATE_PASSED" else state.last_safe_stage,
        last_gate=None,
        gated_main_oid=None,
        gate_feature_identity=None,
        gate_identity_record=None,
        gate_identity_sha256=None,
    )


def _cmd_run_codex_gate(args: ProgArgs) -> int:
    """`run-codex-gate` (T039).

    Pre-attempt guards (pure checks of the loaded record only; a refusal
    here is not a gate attempt and writes nothing): lifecycle state (B2)
    -> no pending checkpoint mutation boundary -> implementation stage.
    Branch and real-staging-area guards run AFTER the revocation below.

    **Latest gate ATTEMPT supersedes prior eligibility (third remediation,
    B1).** Immediately after the guards, BEFORE anything that may fail, the
    previous gate is revoked and that revocation is durably written as a
    safe in-progress state (`RUNNING`, stage at most
    `IMPLEMENTATION_COMPLETE`, every gate-bound field cleared). Every later
    exit - actor failure, parser failure, audit/evidence write failure,
    Candidate Tree failure, identity mismatch, or any unexpected exception
    - therefore leaves the block non-checkpoint-eligible. Only a newly
    completed eligible PASS writes `GATE_PASSED` again, and only after its
    immutable structured gate-identity evidence has been written.

    Base binding (third remediation, M1): before a gate can become
    eligible, `fingerprint.head_oid == expected_head_oid == main HEAD ==
    gated_main_oid` must hold.
    """
    project_root = Path(args.project_root)
    config, warnings = load_config(project_root / ".ai-workflow.toml")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    ai_runs_dir = project_root / config.ai_runs.directory
    state = block_state_mod.load_block_state(ai_runs_dir)
    if state is None:
        raise WorkflowError("no active block; run 'start-block' first")
    _refuse_on_unresolved_mutation_boundary(state, "run-codex-gate")
    if state.state not in _RUN_CODEX_GATE_ELIGIBLE_STATES:
        raise GitAmbiguityError(
            f"run-codex-gate refused: block-state is {state.state!r}, which may not start a new gate "
            f"(eligible: {', '.join(_RUN_CODEX_GATE_ELIGIBLE_STATES)}); a BLOCKED_MANUAL block requires "
            "manual recovery and a fresh Orchestrator decision"
        )
    if state.checkpoint_mutation_boundary is not None:
        raise GitAmbiguityError(
            "run-codex-gate refused: a previous checkpoint attempt failed after mutating Git state "
            f"(boundary {state.checkpoint_mutation_boundary!r}); manual recovery is required"
        )

    if state.last_safe_stage not in _GATE_ELIGIBLE_STAGES:
        raise GitAmbiguityError(
            "run-codex-gate refused: implementation has not been completed for this block yet "
            f"(last_safe_stage={state.last_safe_stage!r}); run 'run-claude' to STATUS: COMPLETE first"
        )

    # B1: the attempt begins here - every check above is a pure check of
    # the loaded record. Revoke and persist BEFORE anything that touches
    # Git, the filesystem, or an actor, so any later failure (including an
    # unexpected exception) leaves the block non-checkpoint-eligible.
    state = replace(_revoke_prior_gate(state), state="RUNNING", updated_at=_utc_now_iso())
    block_state_mod.write_block_state(ai_runs_dir, state)

    repo = git_ops.open_repository(project_root)
    current = repo.current_branch()
    if current != state.branch_name:
        raise GitAmbiguityError(
            f"run-codex-gate refused: current branch {current!r} does not match the active "
            f"block's branch {state.branch_name!r}"
        )
    repo.check_staging_precondition()

    # Item 7: re-validate the persisted feature identity at read time.
    tasks_md_path = _resolve_persisted_tasks_md_path(project_root, state.tasks_md_path)

    # M1: the authorized base must agree everywhere before this gate can
    # ever become eligible.
    main_oid_at_gate = repo.branch_oid(config.project.main_branch)
    head_oid_now = repo.rev_parse("HEAD")
    if state.expected_head_oid is None:
        raise GitAmbiguityError(
            "run-codex-gate refused: no expected_head_oid is recorded for this block - the "
            "authorized base cannot be proven"
        )
    if head_oid_now != state.expected_head_oid:
        raise GitAmbiguityError(
            f"run-codex-gate refused: block HEAD {head_oid_now!r} differs from the expected base "
            f"{state.expected_head_oid!r} recorded at start-block"
        )
    if main_oid_at_gate != state.expected_head_oid:
        raise GitAmbiguityError(
            f"run-codex-gate refused: {config.project.main_branch!r} is at {main_oid_at_gate!r}, not at "
            f"the block's authorized base {state.expected_head_oid!r} - main advanced since start-block"
        )

    # A2: build Candidate Tree X and Fingerprint X FIRST.
    pre_candidate = candidate_tree_mod.build_candidate_tree(repo)
    fingerprint_x = compute_fingerprint(repo, pre_candidate, first_task=state.first_task, last_task=state.last_task)
    if fingerprint_x.head_oid != state.expected_head_oid:
        raise GitAmbiguityError(
            f"run-codex-gate refused: Fingerprint head {fingerprint_x.head_oid!r} differs from the "
            f"expected base {state.expected_head_oid!r}"
        )
    gate_identity = gate_identity_mod.GateIdentity(
        fingerprint=fingerprint_x,
        feature_identity=state.tasks_md_path,
        gated_main_oid=main_oid_at_gate,
    )
    prompt_body = _build_codex_review_prompt(state, repo, tasks_md_path, fingerprint_x)

    header_kwargs = dict(
        tool="codex",
        model=None,
        effort=None,
        session_mode=None,
        prior_session_id=None,
        speckit_involved=True,
        scope=_block_scope(state),
        purpose="independent gate",
        workflow_schema_version=config.workflow.schema_version,
        workflow_version=config.workflow.version,
    )
    # The complete Gate Identity (Fingerprint X + feature + gated main) is
    # stated in both audit halves in canonical JSON, and written as its own
    # immutable, digest-referenced structured evidence for eligible use.
    identity_json = json.dumps(gate_identity_mod.identity_payload(gate_identity), sort_keys=True, indent=2)
    identity_lines = f"Gate Identity:\n```json\n{identity_json}\n```\n"
    prompt_content = audit_mod.render_metadata_header(**header_kwargs) + "\n" + identity_lines + "\n" + prompt_body
    record = audit_mod.create_run_record(
        ai_runs_dir, slug_source=f"{state.branch_name}-run-codex-gate", prompt_content=prompt_content
    )

    def _record_result(outcome_text: str) -> None:
        audit_mod.write_result(
            record,
            audit_mod.render_metadata_header(**header_kwargs, paired_prompt_stem=record.stem)
            + "\n"
            + identity_lines
            + f"\n{outcome_text}\n",
        )

    try:
        session_outcome = codex_actor.run_codex_gate_session(
            prompt_body=prompt_body,
            cwd=str(project_root),
            argv_prefix=_test_argv_prefix(_CODEX_ARGV_PREFIX_ENV),
        )
    except proc.TransientOperationalError as exc:
        block_state_mod.write_block_state(
            ai_runs_dir, replace(state, state="BLOCKED_MANUAL", retry_count_current_stage=1, updated_at=_utc_now_iso())
        )
        _record_result(f"Outcome: BLOCKED_MANUAL (operational failure after single retry)\n\n{exc}")
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MANUAL_INTERVENTION

    post_candidate = candidate_tree_mod.build_candidate_tree(repo)
    if post_candidate.tree_oid != pre_candidate.tree_oid:
        gate_identity_mod.write_gate_evidence(
            record, gate_identity, result="INVALIDATED_WORKING_TREE_MODIFIED", checkpoint_eligible=False
        )
        block_state_mod.write_block_state(
            ai_runs_dir, replace(state, state="QA_REMEDIATION_REQUIRED", updated_at=_utc_now_iso())
        )
        _record_result(
            "Outcome: Codex gate modified the working tree (hard failure, result below is untrusted)\n\n"
            f"RESULT (as reported): {session_outcome.parsed.result}\n\n" + session_outcome.transcript
        )
        print("error: Codex gate modified the working tree", file=sys.stderr)
        return EXIT_NEGATIVE_RESULT

    eligible = codex_actor.is_checkpoint_eligible(session_outcome.parsed, config.checkpoint.blocking_severities)
    evidence_name, evidence_sha256 = gate_identity_mod.write_gate_evidence(
        record, gate_identity, result=session_outcome.parsed.result, checkpoint_eligible=eligible
    )
    _record_result(
        f"RESULT: {session_outcome.parsed.result}\nParsed: {session_outcome.parsed.parsed}\n"
        f"Checkpoint-eligible: {eligible}\n"
        f"Gate identity evidence: {evidence_name} sha256:{evidence_sha256}\n\n" + session_outcome.transcript
    )

    if eligible:
        block_state_mod.write_block_state(
            ai_runs_dir,
            replace(
                state,
                state="RUNNING",
                last_safe_stage="GATE_PASSED",
                last_gate=fingerprint_x,
                gated_main_oid=main_oid_at_gate,
                gate_feature_identity=state.tasks_md_path,
                gate_identity_record=evidence_name,
                gate_identity_sha256=evidence_sha256,
                retry_count_current_stage=0,
                updated_at=_utc_now_iso(),
            ),
        )
        return EXIT_SUCCESS

    block_state_mod.write_block_state(
        ai_runs_dir, replace(state, state="QA_REMEDIATION_REQUIRED", updated_at=_utc_now_iso())
    )
    return EXIT_NEGATIVE_RESULT


_STALE_GATE_MESSAGE_PREFIXES = (
    checkpoint_mod.CHECKPOINT_FINGERPRINT_MISMATCH_MESSAGE,
    checkpoint_mod.CHECKPOINT_MAIN_ADVANCED_MESSAGE,
    checkpoint_mod.CHECKPOINT_BASE_MISMATCH_MESSAGE,
    checkpoint_mod.CHECKPOINT_GATE_EVIDENCE_MESSAGE,
)


def _cmd_checkpoint(args: ProgArgs) -> int:
    """`checkpoint` (T040): delegates the actual flow to `git/checkpoint.py`
    and translates its outcome into block-state + the CLI's exit-code
    convention.

    - A stale-gate block (working tree changed, main advanced, base
      identities disagree, or cached identity not proven by the immutable
      gate evidence) supersedes the recorded gate entirely
      (`_revoke_prior_gate`) - a future `resume`/`checkpoint` must never
      treat it as still valid.
    - Third remediation (M4): a `CheckpointPartialFailureError` - any
      failure after the first real mutation began - records
      `BLOCKED_MANUAL` AND the exact mutation boundary reached
      (`checkpoint_mutation_boundary`); every lifecycle command and
      `resume` then refuse to continue automatically.

    Uses the block's own PERSISTED `tasks_md_path` (A5), re-validated.
    """
    project_root = Path(args.project_root)
    config, warnings = load_config(project_root / ".ai-workflow.toml")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    ai_runs_dir = project_root / config.ai_runs.directory
    state = block_state_mod.load_block_state(ai_runs_dir)
    if state is None:
        raise WorkflowError("no active block; nothing to checkpoint")
    _refuse_on_unresolved_mutation_boundary(state, "checkpoint")

    repo = git_ops.open_repository(project_root)
    tasks_md_path = _resolve_persisted_tasks_md_path(project_root, state.tasks_md_path)

    try:
        outcome = checkpoint_mod.run_checkpoint(repo, config, state, tasks_md_path, ai_runs_dir=ai_runs_dir)
    except checkpoint_mod.CheckpointPartialFailureError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        # B4: the durable pre-mutation marker (BLOCKED_MANUAL +
        # mutation_intent_recorded) is already on disk; this write only
        # refines it with the exact boundary. If it fails, the marker
        # remains in force.
        updated = replace(
            checkpoint_mod.mutation_intent_state(state), checkpoint_mutation_boundary=exc.stage
        )
        try:
            block_state_mod.write_block_state(ai_runs_dir, updated)
        except Exception as write_exc:  # noqa: BLE001 - the durable marker already protects the block
            print(
                f"error: could not durably record the exact mutation boundary ({write_exc}); the "
                "on-disk block-state is either the durable pre-mutation marker or this refined "
                "boundary - both keep the block blocked; manual recovery is required",
                file=sys.stderr,
            )
        return exc.exit_code
    except checkpoint_mod.CheckpointIntentNotRecordedError as exc:
        # Nothing was mutated; never overwrite whatever the failed write
        # may or may not have left behind.
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except checkpoint_mod.CheckpointBlockedError as exc:
        if exc.message.startswith(_STALE_GATE_MESSAGE_PREFIXES):
            block_state_mod.write_block_state(
                ai_runs_dir, replace(_revoke_prior_gate(state), updated_at=_utc_now_iso())
            )
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.exit_code

    # The guard keeps the durable mutation boundary authoritative until the
    # final state has been synced and its receipt becomes visible.
    updated = replace(
        state,
        state="COMPLETED",
        last_safe_stage="CHECKPOINT_COMPLETE",
        checkpoint_mutation_boundary=None,
        updated_at=_utc_now_iso(),
    )
    try:
        block_state_mod.publish_completed_block_state(ai_runs_dir, updated)
    except Exception as write_exc:  # noqa: BLE001 - guard or marker stays authoritative
        print(
            f"error: checkpoint tag {outcome.tag_name} was published, but the final COMPLETED state could "
            f"not be authoritatively published ({write_exc}); the durable checkpoint mutation "
            "boundary remains authoritative even if COMPLETED is visible - manual recovery "
            "and verification are required",
            file=sys.stderr,
        )
        return EXIT_MANUAL_INTERVENTION
    print(
        f"checkpoint created: tag={outcome.tag_name} commit={outcome.checkpoint_commit_oid} "
        f"merge={outcome.merge_commit_sha}"
    )
    return EXIT_SUCCESS


# item 6/A3: the concrete stage `resume` itself executes once
# revalidation succeeds, per `last_safe_stage`. `BRANCH_CREATED` maps to
# `run-claude` NOW (item 6 remediation) because the Orchestrator-approved
# invocation parameters it needs are persisted in advance
# (`BlockState.pending_claude_invocation`, set by `run-claude` itself
# before every attempt) - `resume` REPLAYS that already-authorized
# invocation rather than inventing new parameters.
_RESUME_EXECUTABLE_STAGE: dict[str, str] = {
    "BRANCH_CREATED": "run-claude",
    "IMPLEMENTATION_COMPLETE": "run-codex-gate",
    "CANDIDATE_TREE_BUILT": "run-codex-gate",
    "GATE_PASSED": "checkpoint",
}


def _cmd_resume(args: ProgArgs) -> int:
    """`resume` (T046, A3/item-6 remediation): revalidates a
    `BLOCKED_MANUAL`/`ARCHITECTURAL_DECISION_REQUIRED` block from scratch
    - branch identity, `HEAD` against `BlockState.expected_head_oid`
    (item 6: "every resume revalidates expected HEAD/base identity
    before proceeding", checked for EVERY stage, not merely
    `GATE_PASSED`), and - if `last_safe_stage == GATE_PASSED` - the
    recorded Fingerprint against a freshly rebuilt Candidate Tree,
    exactly as `checkpoint` itself would - rather than trusting the
    persisted file blindly, THEN executes exactly the first incomplete
    authorized stage itself (`contracts/cli-interface.md`'s own literal
    requirement), never more than one stage.

    Item 6: `BRANCH_CREATED` now REPLAYS the block's own persisted
    `pending_claude_invocation` (the Orchestrator-approved model/effort/
    session-mode/purpose `run-claude` recorded before its own most recent
    attempt) - never invents new parameters, never resumes an
    `ARCHITECTURAL_DECISION_REQUIRED` stop this way (that state
    structurally requires a FRESH Orchestrator decision, not a replay of
    the same request - see the dedicated check below). If no pending
    invocation was ever recorded (a record predating this field, or one
    from `regenerate_block_state`'s lossy reconstruction), this still
    fails closed rather than guessing.

    This never treats a *regenerated* (`state/block_state.py`'s
    `regenerate_block_state`, lossy, `last_task=first_task` placeholder,
    no `expected_head_oid`) record as authoritative: `resume` reads ONLY
    the persisted `.ai-runs/.block-state.json` via `load_block_state`
    (never calling `regenerate_block_state` itself), and the mandatory
    `expected_head_oid` check below fails closed for exactly such a
    record (it has none to check against).

    Actor separation is preserved: continuing into `run-claude`/
    `run-codex-gate`/`checkpoint` delegates to those subcommands' own
    handler functions unchanged (the exact same code path a direct
    Orchestrator invocation would take) rather than duplicating or
    reimplementing their logic here, and `resume` stops immediately after
    that ONE delegated call returns - it never chains into a further
    stage on its own initiative (Controlled Automation, constitution
    Principle VII).
    """
    project_root = Path(args.project_root)
    config, warnings = load_config(project_root / ".ai-workflow.toml")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    ai_runs_dir = project_root / config.ai_runs.directory
    state = block_state_mod.load_block_state(ai_runs_dir)
    if state is None:
        print("error: no block-state found; nothing to resume", file=sys.stderr)
        return EXIT_MANUAL_INTERVENTION
    _refuse_on_unresolved_mutation_boundary(state, "resume")
    # Third remediation (M6): `resume` alone never carries a new
    # architectural decision, so it can never move a block out of
    # ARCHITECTURAL_DECISION_REQUIRED. Refuse BEFORE any revalidation or
    # write - the state stays exactly as it is and no actor is invoked.
    if state.state == "ARCHITECTURAL_DECISION_REQUIRED":
        print(
            "error: resume refused: block-state is ARCHITECTURAL_DECISION_REQUIRED - a fresh "
            "Orchestrator architectural decision is required, and 'resume' never replays or "
            "synthesizes one; issue 'run-claude' with new --purpose instructions carrying that "
            "decision (state left unchanged)",
            file=sys.stderr,
        )
        return EXIT_MANUAL_INTERVENTION
    if state.state != "BLOCKED_MANUAL":
        print(
            f"error: resume refused: block-state is {state.state!r}, not BLOCKED_MANUAL",
            file=sys.stderr,
        )
        return EXIT_MANUAL_INTERVENTION
    # Third remediation (M4): a checkpoint that failed after mutating real
    # Git state is never continued automatically - manual recovery first.
    if state.checkpoint_mutation_boundary is not None:
        print(
            "error: resume refused: a previous checkpoint attempt failed after mutating Git state "
            f"(boundary {state.checkpoint_mutation_boundary!r}); manual recovery of the repository "
            "is required before any lifecycle command may continue (state left unchanged)",
            file=sys.stderr,
        )
        return EXIT_MANUAL_INTERVENTION

    repo = git_ops.open_repository(project_root)
    current = repo.current_branch()
    if current != state.branch_name:
        print(
            f"error: resume revalidation failed: current branch {current!r} does not match "
            f"the recorded branch {state.branch_name!r}; resolve manually",
            file=sys.stderr,
        )
        return EXIT_NEGATIVE_RESULT

    # Item 6: universal `HEAD`/base-identity revalidation, for EVERY
    # stage - not merely `GATE_PASSED`. `expected_head_oid` is recorded
    # once, at `start-block` time, and Claude never commits (research.md
    # §0.9), so this stays the expected value through every stage up to
    # `checkpoint`'s own commit. A record with no recorded value at all
    # (predating this field, or `regenerate_block_state`'s lossy
    # reconstruction) cannot be proven and fails closed rather than
    # skipping the check.
    if state.expected_head_oid is None:
        print(
            "error: resume revalidation failed: no expected_head_oid is recorded for this "
            "block (a record predating this field, or a lossy regenerated one) - HEAD "
            "identity cannot be proven; resolve manually",
            file=sys.stderr,
        )
        return EXIT_NEGATIVE_RESULT
    current_head_oid = repo.rev_parse("HEAD")
    if current_head_oid != state.expected_head_oid:
        print(
            f"error: resume revalidation failed: HEAD is {current_head_oid!r}, expected "
            f"{state.expected_head_oid!r} (recorded when this block's branch was created); "
            "resolve manually",
            file=sys.stderr,
        )
        return EXIT_NEGATIVE_RESULT

    if state.last_safe_stage == "GATE_PASSED":
        if state.last_gate is None:
            print(
                "error: resume revalidation failed: last_safe_stage is GATE_PASSED but no "
                "gate is recorded",
                file=sys.stderr,
            )
            return EXIT_NEGATIVE_RESULT
        fresh_candidate = candidate_tree_mod.build_candidate_tree(repo)
        fresh_fingerprint = compute_fingerprint(
            repo, fresh_candidate, first_task=state.first_task, last_task=state.last_task
        )
        if not verify_fingerprint(state.last_gate, fresh_fingerprint):
            print(
                "error: resume revalidation failed: the recorded gate's fingerprint no "
                "longer matches the working tree; re-run 'run-codex-gate' before checkpointing",
                file=sys.stderr,
            )
            return EXIT_NEGATIVE_RESULT

    next_stage = _RESUME_EXECUTABLE_STAGE.get(state.last_safe_stage)
    if next_stage is None:
        print(
            f"error: resume revalidation succeeded, but last_safe_stage={state.last_safe_stage!r} "
            "has no further authorized stage to continue into (this block may already be "
            "complete) - use 'status' to inspect it",
            file=sys.stderr,
        )
        return EXIT_MANUAL_INTERVENTION

    if next_stage == "run-claude":
        # Item 6: replay the block's own persisted, Orchestrator-approved
        # invocation - never invent model/effort/session-mode/purpose.
        pending = state.pending_claude_invocation
        if pending is None:
            print(
                "error: resume revalidation succeeded (state=READY_TO_RESUME), but no pending "
                "Claude invocation is recorded for this block (a record predating this field, "
                "or a lossy regenerated one) to replay - issue 'run-claude' directly with "
                "explicit --model/--effort/--session-mode/--purpose",
                file=sys.stderr,
            )
            updated = replace(state, state="READY_TO_RESUME", retry_count_current_stage=0, updated_at=_utc_now_iso())
            block_state_mod.write_block_state(ai_runs_dir, updated)
            return EXIT_MANUAL_INTERVENTION
        updated = replace(state, state="READY_TO_RESUME", retry_count_current_stage=0, updated_at=_utc_now_iso())
        block_state_mod.write_block_state(ai_runs_dir, updated)
        print("resumed: revalidation succeeded; continuing at 'run-claude' (replaying the authorized invocation)")
        delegate_args = argparse.Namespace(
            project_root=args.project_root,
            model=pending.model,
            effort=pending.effort,
            session_mode=pending.session_mode,
            session_id=pending.session_id,
            purpose=pending.purpose,
            _via_resume=True,
        )
        return _cmd_run_claude(delegate_args)

    updated = replace(state, state="READY_TO_RESUME", retry_count_current_stage=0, updated_at=_utc_now_iso())
    block_state_mod.write_block_state(ai_runs_dir, updated)
    print(f"resumed: revalidation succeeded; continuing at '{next_stage}'")
    delegate_args = argparse.Namespace(project_root=args.project_root)
    if next_stage == "run-codex-gate":
        return _cmd_run_codex_gate(delegate_args)
    return _cmd_checkpoint(delegate_args)


def _cmd_status(args: ProgArgs) -> int:
    """`status` (T050, M4 remediation): read-only report of lock
    ownership, the active block-state (if any), and whether a checkpoint
    would currently be blocked - never mutates anything, and is always
    safe to run regardless of state.

    M4: uses `checkpoint_mod.status_preview` - NOT `checkpoint_mod.
    preflight` - specifically because `preflight` rebuilds the Candidate
    Tree, which creates a real (if disposable) temporary Git index AND a
    real tree object in the object database via `git write-tree`. Those
    are writes, however harmless in practice, and `status` must never
    perform ANY Git-metadata write - see `status_preview`'s own docstring
    for exactly which checks it can and cannot make read-only.
    """
    project_root = Path(args.project_root)
    config, warnings = load_config(project_root / ".ai-workflow.toml")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    ai_runs_dir = project_root / config.ai_runs.directory
    lock_payload = run_lock_mod.peek_lock(ai_runs_dir)
    if lock_payload is None:
        print("Lock: not held")
    else:
        print(
            f"Lock: held by tool={lock_payload.tool} pid={lock_payload.pid} "
            f"hostname={lock_payload.hostname} purpose={lock_payload.purpose!r} "
            f"started_at={lock_payload.started_at}"
        )

    state = block_state_mod.load_block_state(ai_runs_dir)
    if state is None:
        print("Block: none active")
        return EXIT_SUCCESS

    print(json.dumps(block_state_mod.to_dict(state), indent=2, sort_keys=True))

    repo = git_ops.open_repository(project_root)
    print(f"Checkpoint: {checkpoint_mod.status_preview(repo, config, state, ai_runs_dir=ai_runs_dir)}")

    return EXIT_SUCCESS


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Applies the fixed exit-code convention at the boundary.

    `0` success; `1` a substantive negative result (data, not an
    exception); `2` stopped for manual/orchestrator intervention — every
    :class:`WorkflowError` and any other unexpected exception escaping a
    handler maps here.

    This is deliberately the *only* place in the codebase that catches a
    bare :class:`Exception`: every known failure class already has its
    own :class:`WorkflowError` subclass raised from the module that
    detects it (with real type/context preserved there), so nothing
    upstream needs to broadly catch errors itself. What reaches the final
    `except Exception` clause here is, by definition, unexpected — but it
    must still never surface as a `0`/`1` exit or a raw Python traceback
    in normal operation (there is no debug-mode escape hatch in the
    current CLI contract); it gets the same `2` (manual intervention)
    exit as every other non-retryable failure class, with a deterministic
    one-line stderr message.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except WorkflowError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MANUAL_INTERVENTION
    except Exception as exc:  # noqa: BLE001 - CLI boundary of last resort; see docstring above.
        print(f"error: unexpected failure: {exc}", file=sys.stderr)
        return EXIT_MANUAL_INTERVENTION


if __name__ == "__main__":
    raise SystemExit(main())
