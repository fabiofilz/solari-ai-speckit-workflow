"""Unit tests for `config/loader.py` (T009)."""

from __future__ import annotations

from pathlib import Path

import pytest

from solari_workflow.config.loader import load_config
from solari_workflow.errors import ConfigValidationError

VALID_CONFIG = """
[workflow]
schema_version = "1"
source = "https://github.com/fabiofilz/solari-ai-speckit-workflow"
version = "1.0.0"

[project]
name = "example-service"
type = "service"
main_branch = "main"

[speckit]
spec_dir = "specs"

[environment]
stack = "python"
runtime_version = "3.12"
dependency_manager = "uv"
manifest = "pyproject.toml"
lockfile = "uv.lock"
lockfile_must_be_committed = true

[build]
test_command = ["uv", "run", "pytest"]

[git]
checkpoint_merge_strategy = "no-ff"

[git.push]
mode = "manual"

[checkpoint]
tag_prefix = "checkpoint"
blocking_severities = ["blocker", "major"]

[branch_retention]
keep_recent_merged = 2
keep_active = true

[ai_runs]
directory = ".ai-runs"
git_ignored = true
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / ".ai-workflow.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_config_loads_with_no_warnings(tmp_path: Path) -> None:
    path = _write(tmp_path, VALID_CONFIG)
    config, warnings = load_config(path)
    assert warnings == []
    assert config.workflow.schema_version == "1"
    assert config.project.name == "example-service"
    assert config.environment.stack == "python"
    assert config.environment.lockfile == "uv.lock"
    assert config.git.push.mode == "manual"
    assert config.git.checkpoint_merge_strategy == "no-ff"
    assert config.checkpoint.blocking_severities == ["blocker", "major"]
    assert config.build.test_command == ["uv", "run", "pytest"]


def test_parse_error_surfaces_tomllib_message_with_path(tmp_path: Path) -> None:
    path = _write(tmp_path, "this is not [valid toml")
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)


def test_missing_required_fields_are_reported_together(tmp_path: Path) -> None:
    content = """
[workflow]
schema_version = "1"
source = "https://example.invalid"
version = "1.0.0"

[project]
name = "x"
type = "service"
# main_branch missing

[speckit]
spec_dir = "specs"

[environment]
stack = "python"
# runtime_version missing
dependency_manager = "uv"
manifest = "pyproject.toml"
lockfile_must_be_committed = true

[git]
checkpoint_merge_strategy = "no-ff"

[git.push]
mode = "manual"

[checkpoint]
tag_prefix = "checkpoint"
blocking_severities = ["blocker", "major"]

[branch_retention]
keep_recent_merged = 2
keep_active = true

[ai_runs]
directory = ".ai-runs"
git_ignored = true
"""
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "project.main_branch" in problem_paths
    assert "environment.runtime_version" in problem_paths
    # Both problems were reported in ONE aggregated failure, not two separate runs.
    assert len(excinfo.value.problems) >= 2


def test_unrecognized_schema_version_is_a_hard_failure(tmp_path: Path) -> None:
    content = VALID_CONFIG.replace('schema_version = "1"', 'schema_version = "99"')
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "workflow.schema_version" in problem_paths
    message = next(msg for p, msg in excinfo.value.problems if p == "workflow.schema_version")
    assert "99" in message
    assert "1" in message  # names the supported version


def test_missing_schema_version_is_a_hard_failure(tmp_path: Path) -> None:
    content = VALID_CONFIG.replace('schema_version = "1"\n', "")
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "workflow.schema_version" in problem_paths


def test_unknown_key_produces_warning_not_failure(tmp_path: Path) -> None:
    content = VALID_CONFIG + "\n[some_future_table]\nkey = \"value\"\n"
    path = _write(tmp_path, content)
    config, warnings = load_config(path)
    assert config is not None
    assert any("some_future_table" in w for w in warnings)


def test_unknown_key_within_a_known_table_produces_warning_not_failure(tmp_path: Path) -> None:
    content = VALID_CONFIG.replace(
        '[project]\nname = "example-service"',
        '[project]\nname = "example-service"\nfuture_key = "x"',
    )
    path = _write(tmp_path, content)
    config, warnings = load_config(path)
    assert config is not None
    assert any("project.future_key" in w for w in warnings)


def test_invalid_push_mode_is_a_hard_failure_not_covered_by_unknown_key_leniency(
    tmp_path: Path,
) -> None:
    content = VALID_CONFIG.replace('mode = "manual"', 'mode = "auto"')
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "git.push.mode" in problem_paths


def test_invalid_checkpoint_merge_strategy_is_a_hard_failure(tmp_path: Path) -> None:
    content = VALID_CONFIG.replace('checkpoint_merge_strategy = "no-ff"', 'checkpoint_merge_strategy = "ff-only"')
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "git.checkpoint_merge_strategy" in problem_paths


def test_absent_lockfile_is_not_a_loader_error_for_a_stack_conditional_field(tmp_path: Path) -> None:
    content = VALID_CONFIG.replace("lockfile = \"uv.lock\"\n", "")
    path = _write(tmp_path, content)
    config, warnings = load_config(path)
    assert config.environment.lockfile is None


def test_docker_table_present_requires_images(tmp_path: Path) -> None:
    content = VALID_CONFIG + "\n[environment.docker]\n"
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "environment.docker.images" in problem_paths


def test_docker_table_absent_is_fine(tmp_path: Path) -> None:
    path = _write(tmp_path, VALID_CONFIG)
    config, _ = load_config(path)
    assert config.environment.docker is None


def test_docker_table_present_with_images_parses(tmp_path: Path) -> None:
    content = VALID_CONFIG + '\n[environment.docker]\nimages = ["python:3.12.8"]\n'
    path = _write(tmp_path, content)
    config, _ = load_config(path)
    assert config.environment.docker is not None
    assert config.environment.docker.images == ["python:3.12.8"]


def test_lockfile_must_be_committed_false_is_a_hard_failure(tmp_path: Path) -> None:
    """data-model.md: `environment.lockfile_must_be_committed` MUST be `true` in v1 —
    a well-typed `false` is still an invalid value for a known field, not
    covered by the unknown-key leniency rule (Codex-reproduced finding)."""
    content = VALID_CONFIG.replace("lockfile_must_be_committed = true", "lockfile_must_be_committed = false")
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "environment.lockfile_must_be_committed" in problem_paths


def test_keep_active_false_is_a_hard_failure(tmp_path: Path) -> None:
    """data-model.md: `branch_retention.keep_active` MUST be `true` (FR-034/FR-035 —
    the active branch is never a retention candidate)."""
    content = VALID_CONFIG.replace("keep_active = true", "keep_active = false")
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "branch_retention.keep_active" in problem_paths


def test_git_ignored_false_is_a_hard_failure(tmp_path: Path) -> None:
    """data-model.md: `ai_runs.git_ignored` MUST be `true`."""
    content = VALID_CONFIG.replace("git_ignored = true", "git_ignored = false")
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "ai_runs.git_ignored" in problem_paths


def test_tag_prefix_other_than_checkpoint_is_a_hard_failure(tmp_path: Path) -> None:
    """data-model.md: `checkpoint.tag_prefix` MUST be `"checkpoint"` in v1
    (fixed by spec/constitution naming convention) — any other non-empty
    string previously passed the loader's mere "non-empty string" check."""
    content = VALID_CONFIG.replace('tag_prefix = "checkpoint"', 'tag_prefix = "other"')
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "checkpoint.tag_prefix" in problem_paths


def test_valid_fixed_values_still_load_successfully(tmp_path: Path) -> None:
    """Sanity check: the fixed-value validation added above must not reject
    the one value each field is actually required to have."""
    path = _write(tmp_path, VALID_CONFIG)
    config, warnings = load_config(path)
    assert warnings == []
    assert config.environment.lockfile_must_be_committed is True
    assert config.branch_retention.keep_active is True
    assert config.ai_runs.git_ignored is True
    assert config.checkpoint.tag_prefix == "checkpoint"


def test_local_model_table_present_requires_fields(tmp_path: Path) -> None:
    content = VALID_CONFIG + "\n[environment.local_model]\nruntime = \"ollama\"\n"
    path = _write(tmp_path, content)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    problem_paths = {p for p, _ in excinfo.value.problems}
    assert "environment.local_model.runtime_version" in problem_paths
    assert "environment.local_model.model_id" in problem_paths
