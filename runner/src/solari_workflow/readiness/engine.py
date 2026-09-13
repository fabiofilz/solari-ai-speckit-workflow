"""Project Readiness Gate: core engine + common checks (research.md §11).

One core engine, with stack-specific checks registered in a small
internal dict — not a discoverable/dynamic plugin system. `STACK_REGISTRY`
is the dispatch point later phases (T055+) populate; it starts empty
here, so every stack (including `"other"`) runs common checks only until
then, which is exactly what User Story 2's own stack-agnostic
independent test relies on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal

from solari_workflow.config.schema import WorkflowConfig
from solari_workflow.errors import GitAmbiguityError
from solari_workflow.git import ops as git_ops

Status = Literal["PASS", "FAIL", "SKIPPED"]
Severity = Literal["info", "warning", "blocking"]


@dataclass(frozen=True)
class CheckResult:
    """One readiness check's outcome (data-model.md's Readiness Gate Result)."""

    name: str
    status: Status
    message: str
    severity: Severity = "info"


@dataclass(frozen=True)
class ReadinessResult:
    """The full gate result — always includes `SKIPPED` entries for transparency.

    `overall_status` is the ENGINE's own fail-closed verdict — a caller
    that looks only at this field (ignoring `stack_checks_registered`
    entirely) must still never be able to mistake an incomplete result
    for a real PASS:

    - `"FAIL"` — at least one check actually failed.
    - `"INCOMPLETE"` — every check that DID run passed, but the
      project's configured stack needs stack-specific checks
      (`STACK_REGISTRY`, populated starting Phase 6/T055-T063) that are
      not registered yet — a real PASS/FAIL cannot honestly be claimed.
      `"other"` never lands here: common-checks-only is its permanent,
      correct design, not a gap (see `stack_checks_registered`).
    - `"PASS"` — every check that ran passed, AND the stack either needs
      no stack-specific checks (`"other"`) or already has them
      registered.

    `stack_checks_registered` is kept as a separate, purely informational
    field (`True` for `"other"` or any stack `STACK_REGISTRY` already has
    an entry for) — callers should prefer switching on `overall_status`
    alone rather than re-deriving policy from this field, to avoid two
    sources of truth that could disagree.
    """

    overall_status: Literal["PASS", "FAIL", "INCOMPLETE"]
    checks: list[CheckResult]
    checked_at: str
    config_version: str
    stack_checks_registered: bool


CheckFn = Callable[[WorkflowConfig, Path], CheckResult]

# Populated per-stack starting in Phase 6 (T055+): {"python": [...], ...}.
# Any stack with no registry entry yet (including every stack today) runs
# common checks only.
STACK_REGISTRY: dict[str, list[CheckFn]] = {}


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gh[oprsu]_[A-Za-z0-9]{20,}"),
]


def check_config_valid(config: WorkflowConfig, project_root: Path) -> CheckResult:
    """By the time this runs, `config` already parsed/validated successfully.

    `config/loader.py` raises `ConfigValidationError` before the engine
    is ever invoked with an invalid document, so reaching this check at
    all already proves the config is valid.
    """
    return CheckResult("config_valid", "PASS", "`.ai-workflow.toml` parsed and validated.")


def check_ai_runs_git_ignored(config: WorkflowConfig, project_root: Path) -> CheckResult:
    """Proves `.ai-runs/` is actually excluded by Git's ignore rules.

    Deliberately uses `GitRepo.is_path_ignored` (`git check-ignore`)
    rather than merely checking for tracked files under the directory: an
    untracked-but-*not*-ignored directory (e.g. a freshly created,
    never-`git add`-ed `.ai-runs/` with no matching `.gitignore` rule)
    would incorrectly PASS a tracked-files-only check even though it is
    not actually Git-ignored, which is the specific invariant
    `ai_runs.git_ignored = true` (data-model.md) requires. A Git
    inspection error/ambiguity fails closed as a blocking `FAIL`, never a
    silent PASS.
    """
    directory = config.ai_runs.directory
    try:
        repo = git_ops.open_repository(project_root)
        ignored = repo.is_path_ignored(directory)
    except GitAmbiguityError as exc:
        return CheckResult("ai_runs_git_ignored", "FAIL", str(exc), "blocking")
    if not ignored:
        return CheckResult(
            "ai_runs_git_ignored",
            "FAIL",
            f"'{directory}' is not excluded by Git's ignore rules; it MUST be Git-ignored.",
            "blocking",
        )
    return CheckResult("ai_runs_git_ignored", "PASS", f"'{directory}' is Git-ignored.")


def check_main_branch_identified(config: WorkflowConfig, project_root: Path) -> CheckResult:
    name = config.project.main_branch
    if not name:
        return CheckResult("main_branch_identified", "FAIL", "project.main_branch is not set.", "blocking")
    return CheckResult("main_branch_identified", "PASS", f"main branch identified as '{name}'.")


def check_checkpoint_strategy_defined(config: WorkflowConfig, project_root: Path) -> CheckResult:
    strategy = config.git.checkpoint_merge_strategy
    if strategy != "no-ff":
        return CheckResult(
            "checkpoint_strategy_defined",
            "FAIL",
            f"unsupported git.checkpoint_merge_strategy '{strategy}'; v1 supports only 'no-ff'.",
            "blocking",
        )
    return CheckResult("checkpoint_strategy_defined", "PASS", "git.checkpoint_merge_strategy = 'no-ff'.")


def _iter_config_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_config_strings(item)
    elif hasattr(value, "__dataclass_fields__"):
        for field_name in value.__dataclass_fields__:
            yield from _iter_config_strings(getattr(value, field_name))


def check_no_obvious_secrets(config: WorkflowConfig, project_root: Path) -> CheckResult:
    """Best-effort heuristic scan over config string values (config-schema.md's Secrets section).

    Documented explicitly as a heuristic, not a guarantee — no static
    scan can prove the absence of secrets.
    """
    suspicious: list[str] = []
    for text in _iter_config_strings(config):
        if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            suspicious.append(text)
    if suspicious:
        return CheckResult(
            "no_obvious_secrets",
            "FAIL",
            f"config contains {len(suspicious)} suspicious-looking value(s) resembling a "
            "secret (heuristic pattern match, not a guarantee).",
            "warning",
        )
    return CheckResult("no_obvious_secrets", "PASS", "no obvious secret-shaped values found in config.")


COMMON_CHECKS: list[CheckFn] = [
    check_config_valid,
    check_ai_runs_git_ignored,
    check_main_branch_identified,
    check_checkpoint_strategy_defined,
    check_no_obvious_secrets,
]


def run_readiness_gate(config: WorkflowConfig, project_root: Path) -> ReadinessResult:
    """Run every common check, plus the stack's registered checks (if any).

    `stack_checks_registered` is `True` for `"other"` (which needs no
    stack-specific checks, by design, forever) or for any stack
    `STACK_REGISTRY` maps to a NON-EMPTY list of checks; it is `False`
    for every other stack until Phase 6 (T055-T063) registers one or
    more real checks for it — this needs no further change here once
    that happens, since it is derived purely from `STACK_REGISTRY`'s own
    current contents. A bare registry *key* present with an empty list
    (`STACK_REGISTRY["python"] = []`) does NOT count as registered: a key
    alone is not a complete stack readiness implementation, and zero
    checks having run proves nothing about that stack's actual
    readiness — checking `stack in STACK_REGISTRY` alone would let an
    empty placeholder entry silently unlock a `PASS` no check backs.

    `overall_status` is fail-closed at the ENGINE level (not merely at a
    calling CLI subcommand): a `FAIL` among the checks that DID run
    always wins; failing that, an unregistered stack's result is
    `"INCOMPLETE"`, never `"PASS"` — a caller that inspects only
    `overall_status` can never mistake an incomplete readiness answer for
    a real one. Only when every check passed AND the stack's checks are
    registered (or it is `"other"`) is `"PASS"` returned. The full check
    list (including `SKIPPED` entries) is always returned for
    transparency regardless of `overall_status`.
    """
    checks: list[CheckResult] = [fn(config, project_root) for fn in COMMON_CHECKS]
    stack = config.environment.stack
    stack_checks = STACK_REGISTRY.get(stack, [])
    checks.extend(fn(config, project_root) for fn in stack_checks)

    # A non-empty list of checks is what "registered" means — a present
    # but empty registry entry (`STACK_REGISTRY["python"] = []`) is
    # indistinguishable in effect from no entry at all: zero checks ran,
    # so nothing has actually verified that stack's readiness.
    stack_checks_registered = stack == "other" or len(stack_checks) > 0

    overall: Literal["PASS", "FAIL", "INCOMPLETE"]
    if any(c.status == "FAIL" for c in checks):
        overall = "FAIL"
    elif not stack_checks_registered:
        overall = "INCOMPLETE"
    else:
        overall = "PASS"

    return ReadinessResult(
        overall_status=overall,
        checks=checks,
        checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        config_version=config.workflow.version,
        stack_checks_registered=stack_checks_registered,
    )
