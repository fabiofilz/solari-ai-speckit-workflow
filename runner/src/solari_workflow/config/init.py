"""Generic `.ai-workflow.toml` template generation and writing (T023,
User Story 1 — `solari-workflow init`, `contracts/cli-interface.md`).

Renders a schema-valid config document from orchestrator-supplied values
(`InitOptions`) plus a small set of generic, stack-conventional defaults
(e.g. Go's `go.mod`/`go.sum`, Node's `package.json`/`package-lock.json`)
for whatever the orchestrator does not explicitly supply — never a value
specific to any one *consuming* project, and never anything derived from
this workflow's own PDF Converter reference project (spec.md SC-001,
research.md §17's "never guesses a project-specific value").

Ownership stays exactly as `contracts/config-schema.md` states: this is
the **only** runner code path that ever writes `.ai-workflow.toml`, and
only when it does not already exist, or with an explicit `--force` after
showing a diff (`solari_workflow.cli`'s `init` subcommand handler).
"""

from __future__ import annotations

import contextlib
import difflib
import os
import tempfile
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from solari_workflow.config.loader import validate_document
from solari_workflow.config.schema import STACKS, Stack, WorkflowConfig
from solari_workflow.errors import ConfigValidationError

CONFIG_FILENAME = ".ai-workflow.toml"
GITIGNORE_FILENAME = ".gitignore"

# This workflow tool's own canonical repository — not a per-project value;
# every project bootstrapped by this runner records the same `source`
# (research.md §17 point 5), identifying which workflow implementation
# generated its config, not the consuming project itself.
WORKFLOW_SOURCE_URL = "https://github.com/fabiofilz/solari-ai-speckit-workflow"

_DISTRIBUTION_NAME = "solari-workflow"


def _runner_version() -> str:
    """The installed runner's own version (`[workflow] version`,
    research.md §17 point 5) — read from installed package metadata so it
    always tracks `pyproject.toml`'s `[project] version` without a second,
    driftable copy. Falls back to `"0.0.0"` only in the unusual case of
    running against an uninstalled source tree (never expected in normal
    `uv run`/`uv tool install` usage, but must not crash `init` if it
    happens)."""
    try:
        return metadata.version(_DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return "0.0.0"


@dataclass(frozen=True)
class StackDefaults:
    """Generic, stack-conventional (never project-specific) fallback values."""

    runtime_version: str
    dependency_manager: str
    manifest: str
    lockfile: str | None
    test_command: tuple[str, ...] | None


_STACK_DEFAULTS: dict[str, StackDefaults] = {
    "python": StackDefaults("3.12", "uv", "pyproject.toml", "uv.lock", ("uv", "run", "pytest")),
    "node": StackDefaults("20", "npm", "package.json", "package-lock.json", ("npm", "test")),
    "go": StackDefaults("1.22", "go modules", "go.mod", "go.sum", ("go", "test", "./...")),
    "rust": StackDefaults("1.75", "cargo", "Cargo.toml", "Cargo.lock", ("cargo", "test")),
    "other": StackDefaults("unspecified", "unspecified", "unspecified", None, None),
}


@dataclass(frozen=True)
class InitOptions:
    """Every value `init` needs to render a config — orchestrator-supplied
    where given; a generic stack default fills in whatever is left `None`
    (never a project-specific guess)."""

    project_name: str
    stack: Stack
    project_type: str = "service"
    main_branch: str = "main"
    spec_dir: str = "specs"
    runtime_version: str | None = None
    dependency_manager: str | None = None
    manifest: str | None = None
    lockfile: str | None = None

    def __post_init__(self) -> None:
        if self.stack not in STACKS:
            raise ValueError(f"unsupported stack {self.stack!r}; must be one of {', '.join(STACKS)}")


def _toml_string(value: str) -> str:
    """Minimal TOML basic-string escaping — sufficient for the plain
    identifiers/paths this template ever embeds (no control characters or
    embedded quotes are expected from any `InitOptions` field)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_config_toml(options: InitOptions) -> str:
    """Render a complete, schema-valid `.ai-workflow.toml` document."""
    defaults = _STACK_DEFAULTS[options.stack]
    runtime_version = options.runtime_version or defaults.runtime_version
    dependency_manager = options.dependency_manager or defaults.dependency_manager
    manifest = options.manifest or defaults.manifest
    lockfile = options.lockfile if options.lockfile is not None else defaults.lockfile

    lines: list[str] = [
        "[workflow]",
        'schema_version = "1"',
        f"source = {_toml_string(WORKFLOW_SOURCE_URL)}",
        f"version = {_toml_string(_runner_version())}",
        "",
        "[project]",
        f"name = {_toml_string(options.project_name)}",
        f"type = {_toml_string(options.project_type)}",
        f"main_branch = {_toml_string(options.main_branch)}",
        "",
        "[speckit]",
        f"spec_dir = {_toml_string(options.spec_dir)}",
        "",
        "[environment]",
        f"stack = {_toml_string(options.stack)}",
        f"runtime_version = {_toml_string(runtime_version)}",
        f"dependency_manager = {_toml_string(dependency_manager)}",
        f"manifest = {_toml_string(manifest)}",
    ]
    if lockfile is not None:
        lines.append(f"lockfile = {_toml_string(lockfile)}")
    lines.append("lockfile_must_be_committed = true")
    lines.append("")

    if defaults.test_command is not None:
        test_command_toml = ", ".join(_toml_string(part) for part in defaults.test_command)
        lines.extend(["[build]", f"test_command = [{test_command_toml}]", ""])

    lines.extend(
        [
            "[git]",
            'checkpoint_merge_strategy = "no-ff"',
            "",
            "[git.push]",
            'mode = "manual"',
            "",
            "[checkpoint]",
            'tag_prefix = "checkpoint"',
            'blocking_severities = ["blocker", "major"]',
            "",
            "[branch_retention]",
            "keep_recent_merged = 2",
            "keep_active = true",
            "",
            "[ai_runs]",
            'directory = ".ai-runs"',
            "git_ignored = true",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_config(options: InitOptions) -> tuple[str, WorkflowConfig]:
    """Render the TOML document AND validate it through the real config
    loader before returning — a template bug must never write an invalid
    config to disk. Raises :class:`ConfigValidationError` (never expected
    in normal operation) if the rendered template itself fails schema
    validation."""
    content = render_config_toml(options)
    doc = tomllib.loads(content)
    config, warnings = validate_document(doc)
    if config is None:  # pragma: no cover - validate_document raises before this
        raise ConfigValidationError("generated config template failed validation")
    assert not warnings, f"generated config template produced unexpected warnings: {warnings}"
    return content, config


def compute_gitignore_content(existing_content: str | None, ai_runs_directory: str) -> str:
    """Pure computation of the new `.gitignore` content — no I/O.

    `.ai-runs/` (the configured `ai_runs.directory`) covers the lock file
    and block-state file, both living inside that directory, so ignoring
    the directory itself covers both without needing separate entries.
    Never duplicates an already-present entry, and never removes or
    reorders anything already there — `existing_content` is returned
    completely unchanged (not even re-serialized) when the entry is
    already present, and otherwise appended to exactly as an `open(...,
    "a")` append would have produced (T022-T032 re-gate, Finding 5 —
    MINOR: separated from file I/O so both halves of `init`'s write can
    be prepared before either file on disk is touched, for atomicity).
    """
    entry = ai_runs_directory.rstrip("/") + "/"
    if existing_content is None:
        return f"{entry}\n"
    existing_lines = existing_content.splitlines()
    if entry in existing_lines or ai_runs_directory in existing_lines:
        return existing_content
    if existing_content and not existing_content.endswith("\n"):
        return existing_content + f"\n{entry}\n"
    return existing_content + f"{entry}\n"


def atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically.

    Writes to a temporary file in the same directory (so the final
    `os.replace` is an atomic rename on every platform this runner
    supports — same technique as `state/block_state.py`'s
    `write_block_state`) and only then replaces the real path. A
    concurrent reader never observes a partial write, and a failure
    (including `os.replace` itself failing, e.g. because `path` is
    unexpectedly a directory) leaves whatever was already at `path`
    completely untouched. The temporary file is always removed on
    failure, before the exception propagates.
    """
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def restore_or_remove(path: Path, original_content: str | None) -> None:
    """Rollback helper: restore `path` to `original_content`, or remove
    it entirely if it did not exist before (`original_content is None`).

    Used by `cli.py`'s `init` handler to undo a just-written config file
    when a LATER required step (updating `.gitignore`) subsequently
    fails — `init` must never leave a newly created/replaced config
    committed to disk while a later required step failed (T022-T032
    re-gate, Finding 5 — MINOR). Deliberately does not suppress its own
    failure: if rollback itself cannot complete, that is a second,
    distinct problem the caller must surface rather than one silently
    swallowed underneath the original failure.
    """
    if original_content is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_text(path, original_content)


def render_diff(existing_content: str, new_content: str) -> str:
    """Unified diff shown before an `--force` overwrite (`contracts/cli-
    interface.md`: "even then shows a diff before writing")."""
    return "".join(
        difflib.unified_diff(
            existing_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"{CONFIG_FILENAME} (existing)",
            tofile=f"{CONFIG_FILENAME} (new)",
        )
    )
