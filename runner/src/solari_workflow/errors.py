"""Shared exception types and the CLI's fixed exit-code convention.

Per `contracts/cli-interface.md`, every `solari-workflow` subcommand exits:

- ``0`` — success.
- ``1`` — ran to completion, but the substantive result was negative (a
  gate ``FAIL``, a checkpoint block). This is never raised as an
  exception here — call sites return it as data (a result object), since
  it is an expected, non-exceptional outcome.
- ``2`` — stopped for manual/orchestrator intervention. Every
  :class:`WorkflowError` subclass below maps to this exit code, matching
  research.md §0.6's non-retryable failure classes (invalid config, Git
  ambiguity, an unexpected staged state, an unsupported CLI capability).

This module only defines the *types*; nothing here decides retry
behavior (see ``platform/proc.py``'s ``TransientOperationalError``, which
is deliberately NOT a :class:`WorkflowError` subclass since a transient
failure is retried once before it becomes a manual-intervention stop).
"""

from __future__ import annotations

EXIT_SUCCESS = 0
EXIT_NEGATIVE_RESULT = 1
EXIT_MANUAL_INTERVENTION = 2


class WorkflowError(Exception):
    """Base class for every error the CLI boundary knows how to report.

    Every subclass represents a failure class that stops the current
    subcommand for manual/orchestrator intervention (exit code 2) rather
    than being auto-retried — research.md §0.6.
    """

    exit_code: int = EXIT_MANUAL_INTERVENTION

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigValidationError(WorkflowError):
    """One or more `.ai-workflow.toml` problems, aggregated together.

    Carries every collected ``(toml_path, problem)`` pair
    (`contracts/config-schema.md`) rather than stopping at the first
    problem found — research.md §5.
    """

    def __init__(
        self,
        message: str,
        problems: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.problems: list[tuple[str, str]] = list(problems) if problems else []


class GitAmbiguityError(WorkflowError):
    """Unexpected Git state: wrong branch, merge conflict, ambiguous HEAD, etc."""


class SpecKitFeatureAmbiguityError(WorkflowError):
    """More than one Spec Kit feature directory under `[speckit].spec_dir`
    has its own `tasks.md`, and no authoritative field exists anywhere in
    the current config/state/contracts to say which one this block's
    task IDs belong to (A5 remediation — the prior "pick the highest-
    numbered directory" heuristic is removed; guessing risked silently
    resolving a task ID against the wrong feature's `tasks.md`, corrupting
    checkpoint tag metadata with another feature's task titles). Fails
    closed rather than inventing a replacement heuristic.
    """


class SpecKitFeatureIdentityError(WorkflowError):
    """A block's persisted Spec Kit feature identity (`BlockState.
    tasks_md_path`, A5/item-7 remediation) fails an integrity check when
    RE-resolved at a later command:

    - the canonicalized (symlink-resolved) path escapes the project root
      (traversal, or a symlink pointing outside the root);
    - the file it names no longer exists, or no longer resolves to a
      location under the project root;
    - the feature identity recorded alongside a gate (`gate_feature_
      identity`) no longer matches the block's own persisted identity
      (item 8 - the composed Block/Gate Identity check).

    Always fails closed - never silently falls back to `None`/"no
    titles", since these are integrity violations, not an ordinary,
    legitimate absence.
    """


class TaskRangeError(WorkflowError):
    """An Orchestrator-supplied task range is not semantically ordered.

    The Fingerprint model (`fingerprint/engine.py`) validates task-ID
    *shape* (`T<digits>`) but intentionally does not own semantic
    ordering (T022-T032's own design boundary). The lifecycle layer that
    owns an Orchestrator-supplied `--first-task`/`--last-task` pair
    (`start-block`, T037) is what must enforce `first_task <= last_task`
    — never inferred or silently reordered.
    """


class BlockIdentityInputError(WorkflowError):
    """An Orchestrator-supplied block identity value (block name or task
    ID) does not satisfy its strict grammar (fourth remediation, B3).

    Identity values are interpolated into line-oriented Git commit, merge
    and tag messages; anything capable of becoming an additional message
    line (line breaks, control characters) or of forging a message field
    (`:`) is refused when the identity is first accepted, never escaped
    later.
    """


class StagingPreconditionError(GitAmbiguityError):
    """The real index has staged content unrelated to the workflow (research.md §0.8).

    A subtype of :class:`GitAmbiguityError` since it is, specifically, an
    unexpected/ambiguous Git state — never auto-recovered (no ``git
    reset``/``git stash`` performed on the runner's own initiative).
    """

    def __init__(
        self,
        message: str,
        staged_paths: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.staged_paths: list[str] = list(staged_paths) if staged_paths else []


class UnsupportedCLICapabilityError(WorkflowError):
    """The installed `claude`/`codex` CLI lacks a requested capability (research.md §9).

    Also raised by `readiness-check` itself while the stack-specific
    checks registry (Phase 6, T054-T063) is not yet populated for the
    project's configured stack — the CLI is not yet *capable* of
    reporting a complete readiness result for that stack, so it must not
    claim one (see `readiness/engine.py`'s `stack_checks_registered`).
    """


class LockOwnershipError(WorkflowError):
    """`RunLock.release()` refused to delete a lock it cannot prove it owns.

    Raised when this instance either never successfully acquired the
    lock, or the on-disk lock payload's `owner_token` no longer matches
    the token recorded at this instance's own successful `acquire()` —
    meaning the lock file was removed and replaced (by another process,
    or manually) since. Per the single-owner, manual-only stale-lock
    recovery policy (research.md §7), `release()` never deletes a lock
    it cannot verify as its own; it fails closed instead.
    """


def exit_code_for(exc: BaseException) -> int:
    """Map an exception to the CLI's fixed exit-code convention.

    Any :class:`WorkflowError` uses its own ``exit_code``; every other
    exception (a genuinely unexpected error escaping to the CLI boundary)
    falls back to :data:`EXIT_MANUAL_INTERVENTION`, since an unrecognized
    failure is, by definition, not something the runner can safely
    auto-retry or treat as a mere negative result.
    """
    if isinstance(exc, WorkflowError):
        return exc.exit_code
    return EXIT_MANUAL_INTERVENTION
