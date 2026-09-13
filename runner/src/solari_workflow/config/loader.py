"""`.ai-workflow.toml` parsing and validation (research.md §5, contracts/config-schema.md).

- Parsed exclusively with stdlib `tomllib` — the runner never writes this
  file, only reads it (config-schema.md's ownership contract).
- `tomllib.TOMLDecodeError` is surfaced verbatim, with the file path and
  the parser's own line/column position.
- `[workflow].schema_version` is gated: only `"1"` is accepted in v1;
  anything else (including absence) is a hard failure naming the
  supported version(s).
- Every schema problem is collected and reported together as one
  aggregated :class:`~solari_workflow.errors.ConfigValidationError` —
  never stop at the first.
- An unrecognized key or table anywhere is a warning (returned to the
  caller to print), never a hard failure — this keeps the schema
  additive/forward-compatible. An *invalid value* for a *known* field
  (e.g. `git.push.mode = "auto"`) is always a hard failure, since that is
  not covered by the unknown-key leniency rule. This includes every
  field data-model.md fixes to one exact v1 value even though its *type*
  is a plain bool/string rather than an enum — e.g.
  `environment.lockfile_must_be_committed`, `branch_retention.keep_active`,
  `ai_runs.git_ignored` (each MUST be `true`) and `checkpoint.tag_prefix`
  (MUST be `"checkpoint"`) — validated via `_require_fixed_bool`/
  `_require_fixed_str` below, never merely type-checked.
- Stack-conditional fields (e.g. `environment.lockfile`) are never
  required by the loader itself — the readiness gate, not the config
  loader, decides whether their absence is a problem for a given stack.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from solari_workflow.config.schema import (
    AiRunsSection,
    BranchRetentionSection,
    BuildSection,
    CHECKPOINT_MERGE_STRATEGIES,
    CheckpointSection,
    DockerSection,
    EnvVar,
    EnvironmentSection,
    GitPushSection,
    GitSection,
    KnownDeferred,
    LocalModelSection,
    OSDependency,
    ProjectSection,
    PUSH_MODES,
    SUPPORTED_SCHEMA_VERSIONS,
    STACKS,
    SpeckitSection,
    WorkflowConfig,
    WorkflowSection,
)
from solari_workflow.errors import ConfigValidationError


class _Problems:
    """Aggregates `(toml_path, problem)` pairs across the whole validation pass."""

    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []

    def add(self, path: str, problem: str) -> None:
        self._items.append((path, problem))

    def __bool__(self) -> bool:
        return bool(self._items)

    @property
    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def raise_if_any(self) -> None:
        if not self._items:
            return
        summary = "; ".join(f"{path}: {problem}" for path, problem in self._items)
        raise ConfigValidationError(
            f"{len(self._items)} configuration problem(s) found: {summary}",
            problems=self._items,
        )


class _Warnings:
    """Collects human-readable warnings (unknown keys/tables) to print, never fail on."""

    def __init__(self) -> None:
        self._items: list[str] = []

    def add(self, message: str) -> None:
        self._items.append(message)

    @property
    def items(self) -> list[str]:
        return list(self._items)


def parse_toml_file(path: Path) -> dict[str, Any]:
    """Parse `path` as TOML, surfacing `tomllib.TOMLDecodeError` verbatim."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigValidationError(f"could not read config file {path}: {exc}") from exc
    try:
        return tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigValidationError(f"{path}: {exc}") from exc


def _get_table(doc: dict[str, Any], key: str, path: str, problems: _Problems) -> dict[str, Any] | None:
    if key not in doc:
        problems.add(path, "missing required table")
        return None
    value = doc[key]
    if not isinstance(value, dict):
        problems.add(path, f"expected a table, got {type(value).__name__}")
        return None
    return value


def _require_str(table: dict[str, Any], key: str, path: str, problems: _Problems) -> str | None:
    if key not in table:
        problems.add(path, "missing required field")
        return None
    value = table[key]
    if not isinstance(value, str) or not value:
        problems.add(path, f"expected a non-empty string, got {value!r}")
        return None
    return value


def _optional_str(table: dict[str, Any], key: str, path: str, problems: _Problems) -> str | None:
    if key not in table or table[key] is None:
        return None
    value = table[key]
    if not isinstance(value, str):
        problems.add(path, f"expected a string, got {value!r}")
        return None
    return value


def _require_bool(table: dict[str, Any], key: str, path: str, problems: _Problems) -> bool | None:
    if key not in table:
        problems.add(path, "missing required field")
        return None
    value = table[key]
    if not isinstance(value, bool):
        problems.add(path, f"expected a boolean, got {value!r}")
        return None
    return value


def _require_int(table: dict[str, Any], key: str, path: str, problems: _Problems) -> int | None:
    if key not in table:
        problems.add(path, "missing required field")
        return None
    value = table[key]
    if not isinstance(value, int) or isinstance(value, bool):
        problems.add(path, f"expected an integer, got {value!r}")
        return None
    return value


def _require_list_of_str(table: dict[str, Any], key: str, path: str, problems: _Problems) -> list[str] | None:
    if key not in table:
        problems.add(path, "missing required field")
        return None
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.add(path, f"expected an array of strings, got {value!r}")
        return None
    return list(value)


def _optional_command(table: dict[str, Any], key: str, path: str, problems: _Problems) -> list[str] | str | None:
    if key not in table or table[key] is None:
        return None
    value = table[key]
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    problems.add(path, f"expected a string or an array of strings, got {value!r}")
    return None


def _require_enum(
    table: dict[str, Any],
    key: str,
    path: str,
    allowed: tuple[str, ...],
    problems: _Problems,
) -> str | None:
    """A known field whose accepted value set is fixed for this schema version.

    An invalid value here is always a hard failure — this is NOT covered
    by the unknown-*key* leniency rule (config-schema.md).
    """
    value = _require_str(table, key, path, problems)
    if value is None:
        return None
    if value not in allowed:
        problems.add(path, f"invalid value {value!r}; supported: {', '.join(allowed)}")
        return None
    return value


def _require_fixed_bool(
    table: dict[str, Any], key: str, path: str, *, expected: bool, problems: _Problems
) -> bool | None:
    """A known boolean field whose value is fixed by the v1 contract (data-model.md).

    e.g. `environment.lockfile_must_be_committed`, `branch_retention.keep_active`,
    `ai_runs.git_ignored` — each MUST be `true` in v1. Present with the
    wrong (but well-typed) value is a hard failure, exactly like
    `_require_enum`'s fixed-value string fields: this is a *known* field
    whose accepted value set is fixed, not an unknown key covered by the
    unknown-key leniency rule (contracts/config-schema.md).
    """
    value = _require_bool(table, key, path, problems)
    if value is None:
        return None
    if value is not expected:
        problems.add(path, f"must be {expected!r} in v1 (got {value!r})")
        return None
    return value


def _require_fixed_str(
    table: dict[str, Any], key: str, path: str, *, expected: str, problems: _Problems
) -> str | None:
    """A known string field whose value is fixed by the v1 contract (data-model.md).

    e.g. `checkpoint.tag_prefix`, which MUST be `"checkpoint"` in v1
    (fixed by spec/constitution naming convention) — not merely "some
    non-empty string".
    """
    value = _require_str(table, key, path, problems)
    if value is None:
        return None
    if value != expected:
        problems.add(path, f"must be {expected!r} in v1 (got {value!r})")
        return None
    return value


def _warn_unknown_keys(
    table: dict[str, Any],
    known: set[str],
    path_prefix: str,
    warnings: _Warnings,
) -> None:
    for key in table:
        if key not in known:
            warnings.add(f"unrecognized key or table '{path_prefix}.{key}' (ignored)")


_TOP_LEVEL_KNOWN = {
    "workflow",
    "project",
    "speckit",
    "environment",
    "build",
    "git",
    "checkpoint",
    "branch_retention",
    "ai_runs",
    "known_deferred",
}
_WORKFLOW_KNOWN = {"schema_version", "source", "version"}
_PROJECT_KNOWN = {"name", "type", "main_branch"}
_SPECKIT_KNOWN = {"spec_dir", "status"}
_ENVIRONMENT_KNOWN = {
    "stack",
    "runtime_version",
    "dependency_manager",
    "manifest",
    "lockfile",
    "lockfile_must_be_committed",
    "docker",
    "local_model",
    "os_dependencies",
    "env_vars",
}
_DOCKER_KNOWN = {"images", "compose_file"}
_LOCAL_MODEL_KNOWN = {"runtime", "runtime_version", "model_id", "digest"}
_OS_DEPENDENCY_KNOWN = {"name", "version"}
_ENV_VAR_KNOWN = {"name", "example"}
_BUILD_KNOWN = {"test_command", "build_command", "lint_command"}
_GIT_KNOWN = {"checkpoint_merge_strategy", "push"}
_GIT_PUSH_KNOWN = {"mode"}
_CHECKPOINT_KNOWN = {"tag_prefix", "blocking_severities"}
_BRANCH_RETENTION_KNOWN = {"keep_recent_merged", "keep_active"}
_AI_RUNS_KNOWN = {"directory", "git_ignored"}
_KNOWN_DEFERRED_ENTRY_KNOWN = {"id", "reason", "owner"}


def _validate_workflow_section(doc: dict[str, Any], problems: _Problems, warnings: _Warnings) -> WorkflowSection | None:
    table = _get_table(doc, "workflow", "workflow", problems)
    if table is None:
        return None
    _warn_unknown_keys(table, _WORKFLOW_KNOWN, "workflow", warnings)

    schema_version = table.get("schema_version")
    if schema_version is None or not isinstance(schema_version, str):
        problems.add(
            "workflow.schema_version",
            f"missing or invalid; supported version(s): {', '.join(SUPPORTED_SCHEMA_VERSIONS)}",
        )
        schema_version = None
    elif schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        problems.add(
            "workflow.schema_version",
            f"unrecognized value {schema_version!r}; supported version(s): "
            f"{', '.join(SUPPORTED_SCHEMA_VERSIONS)}",
        )
        schema_version = None

    source = _require_str(table, "source", "workflow.source", problems)
    version = _require_str(table, "version", "workflow.version", problems)

    if schema_version is None or source is None or version is None:
        return None
    return WorkflowSection(schema_version=schema_version, source=source, version=version)


def _validate_project_section(doc: dict[str, Any], problems: _Problems, warnings: _Warnings) -> ProjectSection | None:
    table = _get_table(doc, "project", "project", problems)
    if table is None:
        return None
    _warn_unknown_keys(table, _PROJECT_KNOWN, "project", warnings)
    name = _require_str(table, "name", "project.name", problems)
    type_ = _require_str(table, "type", "project.type", problems)
    main_branch = _require_str(table, "main_branch", "project.main_branch", problems)
    if name is None or type_ is None or main_branch is None:
        return None
    return ProjectSection(name=name, type=type_, main_branch=main_branch)


def _validate_speckit_section(doc: dict[str, Any], problems: _Problems, warnings: _Warnings) -> SpeckitSection | None:
    table = _get_table(doc, "speckit", "speckit", problems)
    if table is None:
        return None
    _warn_unknown_keys(table, _SPECKIT_KNOWN, "speckit", warnings)
    spec_dir = _require_str(table, "spec_dir", "speckit.spec_dir", problems)
    status = _optional_str(table, "status", "speckit.status", problems)
    if spec_dir is None:
        return None
    return SpeckitSection(spec_dir=spec_dir, status=status)


def _validate_docker_section(env_table: dict[str, Any], problems: _Problems, warnings: _Warnings) -> DockerSection | None:
    if "docker" not in env_table:
        return None
    table = env_table["docker"]
    if not isinstance(table, dict):
        problems.add("environment.docker", f"expected a table, got {table!r}")
        return None
    _warn_unknown_keys(table, _DOCKER_KNOWN, "environment.docker", warnings)
    images = _require_list_of_str(table, "images", "environment.docker.images", problems)
    compose_file = _optional_str(table, "compose_file", "environment.docker.compose_file", problems)
    if images is None:
        return None
    return DockerSection(images=images, compose_file=compose_file)


def _validate_local_model_section(
    env_table: dict[str, Any], problems: _Problems, warnings: _Warnings
) -> LocalModelSection | None:
    if "local_model" not in env_table:
        return None
    table = env_table["local_model"]
    if not isinstance(table, dict):
        problems.add("environment.local_model", f"expected a table, got {table!r}")
        return None
    _warn_unknown_keys(table, _LOCAL_MODEL_KNOWN, "environment.local_model", warnings)
    runtime = _require_str(table, "runtime", "environment.local_model.runtime", problems)
    runtime_version = _require_str(
        table, "runtime_version", "environment.local_model.runtime_version", problems
    )
    model_id = _require_str(table, "model_id", "environment.local_model.model_id", problems)
    digest = _optional_str(table, "digest", "environment.local_model.digest", problems)
    if runtime is None or runtime_version is None or model_id is None:
        return None
    return LocalModelSection(
        runtime=runtime, runtime_version=runtime_version, model_id=model_id, digest=digest
    )


def _validate_os_dependencies(env_table: dict[str, Any], problems: _Problems, warnings: _Warnings) -> list[OSDependency]:
    if "os_dependencies" not in env_table:
        return []
    entries = env_table["os_dependencies"]
    if not isinstance(entries, list):
        problems.add("environment.os_dependencies", f"expected an array of tables, got {entries!r}")
        return []
    result: list[OSDependency] = []
    for index, entry in enumerate(entries):
        path = f"environment.os_dependencies[{index}]"
        if not isinstance(entry, dict):
            problems.add(path, f"expected a table, got {entry!r}")
            continue
        _warn_unknown_keys(entry, _OS_DEPENDENCY_KNOWN, path, warnings)
        name = _require_str(entry, "name", f"{path}.name", problems)
        version = _require_str(entry, "version", f"{path}.version", problems)
        if name is not None and version is not None:
            result.append(OSDependency(name=name, version=version))
    return result


def _validate_env_vars(env_table: dict[str, Any], problems: _Problems, warnings: _Warnings) -> list[EnvVar]:
    if "env_vars" not in env_table:
        return []
    entries = env_table["env_vars"]
    if not isinstance(entries, list):
        problems.add("environment.env_vars", f"expected an array of tables, got {entries!r}")
        return []
    result: list[EnvVar] = []
    for index, entry in enumerate(entries):
        path = f"environment.env_vars[{index}]"
        if not isinstance(entry, dict):
            problems.add(path, f"expected a table, got {entry!r}")
            continue
        _warn_unknown_keys(entry, _ENV_VAR_KNOWN, path, warnings)
        name = _require_str(entry, "name", f"{path}.name", problems)
        example = _require_str(entry, "example", f"{path}.example", problems)
        if name is not None and example is not None:
            result.append(EnvVar(name=name, example=example))
    return result


def _validate_environment_section(
    doc: dict[str, Any], problems: _Problems, warnings: _Warnings
) -> EnvironmentSection | None:
    table = _get_table(doc, "environment", "environment", problems)
    if table is None:
        return None
    _warn_unknown_keys(table, _ENVIRONMENT_KNOWN, "environment", warnings)

    stack = _require_enum(table, "stack", "environment.stack", STACKS, problems)
    runtime_version = _require_str(table, "runtime_version", "environment.runtime_version", problems)
    dependency_manager = _require_str(
        table, "dependency_manager", "environment.dependency_manager", problems
    )
    manifest = _require_str(table, "manifest", "environment.manifest", problems)
    lockfile_must_be_committed = _require_fixed_bool(
        table,
        "lockfile_must_be_committed",
        "environment.lockfile_must_be_committed",
        expected=True,
        problems=problems,
    )
    # Stack-conditional: never required at the loader level (config-schema.md) —
    # the readiness gate decides whether an absent lockfile is a problem.
    lockfile = _optional_str(table, "lockfile", "environment.lockfile", problems)
    docker = _validate_docker_section(table, problems, warnings)
    local_model = _validate_local_model_section(table, problems, warnings)
    os_dependencies = _validate_os_dependencies(table, problems, warnings)
    env_vars = _validate_env_vars(table, problems, warnings)

    if (
        stack is None
        or runtime_version is None
        or dependency_manager is None
        or manifest is None
        or lockfile_must_be_committed is None
    ):
        return None
    return EnvironmentSection(
        stack=stack,  # type: ignore[arg-type]
        runtime_version=runtime_version,
        dependency_manager=dependency_manager,
        manifest=manifest,
        lockfile_must_be_committed=lockfile_must_be_committed,
        lockfile=lockfile,
        docker=docker,
        local_model=local_model,
        os_dependencies=os_dependencies,
        env_vars=env_vars,
    )


def _validate_build_section(doc: dict[str, Any], problems: _Problems, warnings: _Warnings) -> BuildSection:
    if "build" not in doc:
        return BuildSection()
    table = doc["build"]
    if not isinstance(table, dict):
        problems.add("build", f"expected a table, got {table!r}")
        return BuildSection()
    _warn_unknown_keys(table, _BUILD_KNOWN, "build", warnings)
    return BuildSection(
        test_command=_optional_command(table, "test_command", "build.test_command", problems),
        build_command=_optional_command(table, "build_command", "build.build_command", problems),
        lint_command=_optional_command(table, "lint_command", "build.lint_command", problems),
    )


def _validate_git_section(doc: dict[str, Any], problems: _Problems, warnings: _Warnings) -> GitSection | None:
    table = _get_table(doc, "git", "git", problems)
    if table is None:
        return None
    _warn_unknown_keys(table, _GIT_KNOWN, "git", warnings)
    strategy = _require_enum(
        table,
        "checkpoint_merge_strategy",
        "git.checkpoint_merge_strategy",
        CHECKPOINT_MERGE_STRATEGIES,
        problems,
    )
    push_table = _get_table(table, "push", "git.push", problems)
    push: GitPushSection | None = None
    if push_table is not None:
        _warn_unknown_keys(push_table, _GIT_PUSH_KNOWN, "git.push", warnings)
        mode = _require_enum(push_table, "mode", "git.push.mode", PUSH_MODES, problems)
        if mode is not None:
            push = GitPushSection(mode=mode)  # type: ignore[arg-type]

    if strategy is None or push is None:
        return None
    return GitSection(checkpoint_merge_strategy=strategy, push=push)  # type: ignore[arg-type]


def _validate_checkpoint_section(
    doc: dict[str, Any], problems: _Problems, warnings: _Warnings
) -> CheckpointSection | None:
    table = _get_table(doc, "checkpoint", "checkpoint", problems)
    if table is None:
        return None
    _warn_unknown_keys(table, _CHECKPOINT_KNOWN, "checkpoint", warnings)
    tag_prefix = _require_fixed_str(
        table, "tag_prefix", "checkpoint.tag_prefix", expected="checkpoint", problems=problems
    )
    blocking_severities = _require_list_of_str(
        table, "blocking_severities", "checkpoint.blocking_severities", problems
    )
    if tag_prefix is None or blocking_severities is None:
        return None
    return CheckpointSection(tag_prefix=tag_prefix, blocking_severities=blocking_severities)


def _validate_branch_retention_section(
    doc: dict[str, Any], problems: _Problems, warnings: _Warnings
) -> BranchRetentionSection | None:
    table = _get_table(doc, "branch_retention", "branch_retention", problems)
    if table is None:
        return None
    _warn_unknown_keys(table, _BRANCH_RETENTION_KNOWN, "branch_retention", warnings)
    keep_recent_merged = _require_int(
        table, "keep_recent_merged", "branch_retention.keep_recent_merged", problems
    )
    keep_active = _require_fixed_bool(
        table, "keep_active", "branch_retention.keep_active", expected=True, problems=problems
    )
    if keep_recent_merged is None or keep_active is None:
        return None
    return BranchRetentionSection(keep_recent_merged=keep_recent_merged, keep_active=keep_active)


def _validate_ai_runs_section(doc: dict[str, Any], problems: _Problems, warnings: _Warnings) -> AiRunsSection | None:
    table = _get_table(doc, "ai_runs", "ai_runs", problems)
    if table is None:
        return None
    _warn_unknown_keys(table, _AI_RUNS_KNOWN, "ai_runs", warnings)
    directory = _require_str(table, "directory", "ai_runs.directory", problems)
    git_ignored = _require_fixed_bool(
        table, "git_ignored", "ai_runs.git_ignored", expected=True, problems=problems
    )
    if directory is None or git_ignored is None:
        return None
    return AiRunsSection(directory=directory, git_ignored=git_ignored)


def _validate_known_deferred(doc: dict[str, Any], problems: _Problems, warnings: _Warnings) -> list[KnownDeferred]:
    if "known_deferred" not in doc:
        return []
    entries = doc["known_deferred"]
    if not isinstance(entries, list):
        problems.add("known_deferred", f"expected an array of tables, got {entries!r}")
        return []
    result: list[KnownDeferred] = []
    for index, entry in enumerate(entries):
        path = f"known_deferred[{index}]"
        if not isinstance(entry, dict):
            problems.add(path, f"expected a table, got {entry!r}")
            continue
        _warn_unknown_keys(entry, _KNOWN_DEFERRED_ENTRY_KNOWN, path, warnings)
        id_ = _require_str(entry, "id", f"{path}.id", problems)
        reason = _require_str(entry, "reason", f"{path}.reason", problems)
        owner = _require_str(entry, "owner", f"{path}.owner", problems)
        if id_ is not None and reason is not None and owner is not None:
            result.append(KnownDeferred(id=id_, reason=reason, owner=owner))
    return result


def validate_document(doc: dict[str, Any]) -> tuple[WorkflowConfig | None, list[str]]:
    """Validate an already-parsed TOML document.

    Returns `(config, warnings)`. Raises :class:`ConfigValidationError`
    (with every collected problem attached) if any required field is
    missing/malformed or `schema_version`/a fixed-enum field is invalid.
    """
    problems = _Problems()
    warnings = _Warnings()

    _warn_unknown_keys(doc, _TOP_LEVEL_KNOWN, "", warnings)

    workflow = _validate_workflow_section(doc, problems, warnings)
    project = _validate_project_section(doc, problems, warnings)
    speckit = _validate_speckit_section(doc, problems, warnings)
    environment = _validate_environment_section(doc, problems, warnings)
    build = _validate_build_section(doc, problems, warnings)
    git = _validate_git_section(doc, problems, warnings)
    checkpoint = _validate_checkpoint_section(doc, problems, warnings)
    branch_retention = _validate_branch_retention_section(doc, problems, warnings)
    ai_runs = _validate_ai_runs_section(doc, problems, warnings)
    known_deferred = _validate_known_deferred(doc, problems, warnings)

    problems.raise_if_any()

    assert workflow is not None
    assert project is not None
    assert speckit is not None
    assert environment is not None
    assert git is not None
    assert checkpoint is not None
    assert branch_retention is not None
    assert ai_runs is not None

    config = WorkflowConfig(
        workflow=workflow,
        project=project,
        speckit=speckit,
        environment=environment,
        git=git,
        checkpoint=checkpoint,
        branch_retention=branch_retention,
        ai_runs=ai_runs,
        build=build,
        known_deferred=known_deferred,
    )
    return config, warnings.items


def load_config(path: Path) -> tuple[WorkflowConfig, list[str]]:
    """Parse and validate `.ai-workflow.toml` at `path`.

    Returns `(config, warnings)` on success. Raises
    :class:`ConfigValidationError` (aggregating every problem found) on
    any parse or schema failure.
    """
    doc = parse_toml_file(path)
    config, warnings = validate_document(doc)
    assert config is not None  # validate_document raises before returning None here
    return config, warnings
