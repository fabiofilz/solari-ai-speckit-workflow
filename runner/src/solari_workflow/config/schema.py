"""Config dataclasses mirroring data-model.md's "Project Workflow
Configuration" and "Environment Contract" field tables.

Required/optional/stack-conditional structure is preserved as real
fields (not merely comments): a required field has no default, an
optional or stack-conditional one defaults to ``None``/empty. Nothing
here performs validation — `config/loader.py` is what enforces
data-model.md's required/optional/enum rules and raises
:class:`~solari_workflow.errors.ConfigValidationError`; this module only
defines the shape the loader produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Stack = Literal["python", "node", "go", "rust", "other"]
PushMode = Literal["manual"]
CheckpointMergeStrategy = Literal["no-ff"]

STACKS: tuple[Stack, ...] = ("python", "node", "go", "rust", "other")
PUSH_MODES: tuple[PushMode, ...] = ("manual",)
CHECKPOINT_MERGE_STRATEGIES: tuple[CheckpointMergeStrategy, ...] = ("no-ff",)
SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = ("1",)


@dataclass(frozen=True)
class WorkflowSection:
    """`[workflow]` — required."""

    schema_version: str
    source: str
    version: str


@dataclass(frozen=True)
class ProjectSection:
    """`[project]` — required."""

    name: str
    type: str
    main_branch: str


@dataclass(frozen=True)
class SpeckitSection:
    """`[speckit]` — required table; `status` is optional within it."""

    spec_dir: str
    status: str | None = None


@dataclass(frozen=True)
class DockerSection:
    """`[environment.docker]` — optional table; fields required if present."""

    images: list[str] = field(default_factory=list)
    compose_file: str | None = None


@dataclass(frozen=True)
class LocalModelSection:
    """`[environment.local_model]` — optional table; fields required if present."""

    runtime: str = ""
    runtime_version: str = ""
    model_id: str = ""
    digest: str | None = None


@dataclass(frozen=True)
class OSDependency:
    """One entry of `environment.os_dependencies`."""

    name: str
    version: str


@dataclass(frozen=True)
class EnvVar:
    """One entry of `environment.env_vars` — a safe example value only."""

    name: str
    example: str


@dataclass(frozen=True)
class EnvironmentSection:
    """`[environment]` — required table; several fields are stack-conditional."""

    stack: Stack
    runtime_version: str
    dependency_manager: str
    manifest: str
    lockfile_must_be_committed: bool
    lockfile: str | None = None  # stack-conditional (data-model.md)
    docker: DockerSection | None = None
    local_model: LocalModelSection | None = None
    os_dependencies: list[OSDependency] = field(default_factory=list)
    env_vars: list[EnvVar] = field(default_factory=list)


@dataclass(frozen=True)
class BuildSection:
    """`[build]` — entirely optional; absent commands mean a readiness SKIPPED, not FAIL."""

    test_command: list[str] | str | None = None
    build_command: list[str] | str | None = None
    lint_command: list[str] | str | None = None


@dataclass(frozen=True)
class GitPushSection:
    """`[git.push]` — required; v1 recognizes only `mode = "manual"`."""

    mode: PushMode


@dataclass(frozen=True)
class GitSection:
    """`[git]` — required."""

    checkpoint_merge_strategy: CheckpointMergeStrategy
    push: GitPushSection


@dataclass(frozen=True)
class CheckpointSection:
    """`[checkpoint]` — required."""

    tag_prefix: str
    blocking_severities: list[str]


@dataclass(frozen=True)
class BranchRetentionSection:
    """`[branch_retention]` — required."""

    keep_recent_merged: int
    keep_active: bool


@dataclass(frozen=True)
class AiRunsSection:
    """`[ai_runs]` — required."""

    directory: str
    git_ignored: bool


@dataclass(frozen=True)
class KnownDeferred:
    """One entry of the optional top-level `known_deferred` array."""

    id: str
    reason: str
    owner: str


@dataclass(frozen=True)
class WorkflowConfig:
    """The full, validated `.ai-workflow.toml` document."""

    workflow: WorkflowSection
    project: ProjectSection
    speckit: SpeckitSection
    environment: EnvironmentSection
    git: GitSection
    checkpoint: CheckpointSection
    branch_retention: BranchRetentionSection
    ai_runs: AiRunsSection
    build: BuildSection = field(default_factory=BuildSection)
    known_deferred: list[KnownDeferred] = field(default_factory=list)
