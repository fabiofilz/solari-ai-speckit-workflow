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
