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
import sys
from pathlib import Path

from solari_workflow.config import init as config_init
from solari_workflow.config.loader import load_config
from solari_workflow.errors import (
    EXIT_MANUAL_INTERVENTION,
    EXIT_NEGATIVE_RESULT,
    EXIT_SUCCESS,
    UnsupportedCLICapabilityError,
    WorkflowError,
)
from solari_workflow.readiness.engine import run_readiness_gate

ProgArgs = argparse.Namespace


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
    start_block_parser.set_defaults(handler=_cmd_not_implemented)

    run_claude_parser = subparsers.add_parser(
        "run-claude", help="Invoke Claude for one implementation/remediation execution"
    )
    run_claude_parser.add_argument("--model", required=True)
    run_claude_parser.add_argument("--effort", required=True)
    run_claude_parser.add_argument("--session-mode", required=True, choices=["NEW", "CONTINUE"])
    run_claude_parser.add_argument("--session-id", default=None)
    run_claude_parser.add_argument("--purpose", required=True)
    run_claude_parser.set_defaults(handler=_cmd_not_implemented)

    run_codex_gate_parser = subparsers.add_parser(
        "run-codex-gate", help="Invoke Codex for one independent, read-only gate"
    )
    run_codex_gate_parser.set_defaults(handler=_cmd_not_implemented)

    checkpoint_parser = subparsers.add_parser(
        "checkpoint", help="Stage, commit, merge, tag, and retain the current block"
    )
    checkpoint_parser.set_defaults(handler=_cmd_not_implemented)

    resume_parser = subparsers.add_parser(
        "resume", help="Revalidate and continue an interrupted block from its last safe stage"
    )
    resume_parser.set_defaults(handler=_cmd_not_implemented)

    retention_parser = subparsers.add_parser(
        "retention", help="Run branch retention standalone"
    )
    retention_parser.add_argument("--dry-run", action="store_true")
    retention_parser.set_defaults(handler=_cmd_not_implemented)

    status_parser = subparsers.add_parser(
        "status", help="Read-only report of lock ownership and the active block's state"
    )
    status_parser.set_defaults(handler=_cmd_not_implemented)

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
