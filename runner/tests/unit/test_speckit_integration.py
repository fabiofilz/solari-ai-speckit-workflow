"""Unit tests for `speckit/integration.py` against fixture Spec Kit trees (T014)."""

from __future__ import annotations

from pathlib import Path

from solari_workflow.speckit.integration import discover_artifacts, lookup_task_titles

SPEC_WITH_CLARIFICATIONS = """\
# Feature Spec

## Clarifications

### Session 2026-01-01
- Q: something? -> A: yes

## User Scenarios
...
"""

SPEC_WITHOUT_CLARIFICATIONS_CONTENT = """\
# Feature Spec

## Clarifications

## User Scenarios
...
"""

SPEC_WITH_NO_CLARIFICATIONS_SECTION_AT_ALL = """\
# Feature Spec

## User Scenarios
...
"""

TASKS_MD = """\
## Phase 1

- [ ] T001 Do the first thing
- [ ] T002 [P] Do the second thing
- [ ] T003 [P] [US1] Do the third thing
"""


def test_discover_artifacts_all_present(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs" / "001-feature"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text(SPEC_WITH_CLARIFICATIONS, encoding="utf-8")
    (spec_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (spec_dir / "tasks.md").write_text(TASKS_MD, encoding="utf-8")

    artifacts = discover_artifacts(spec_dir)

    assert artifacts.specification_present is True
    assert artifacts.clarification_present is True
    assert artifacts.plan_present is True
    assert artifacts.tasks_present is True


def test_discover_artifacts_partially_present(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs" / "001-feature"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text(SPEC_WITHOUT_CLARIFICATIONS_CONTENT, encoding="utf-8")
    # No plan.md, no tasks.md.

    artifacts = discover_artifacts(spec_dir)

    assert artifacts.specification_present is True
    assert artifacts.clarification_present is False  # heading present but empty
    assert artifacts.plan_present is False
    assert artifacts.tasks_present is False


def test_discover_artifacts_entirely_absent(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs" / "001-feature"
    spec_dir.mkdir(parents=True)

    artifacts = discover_artifacts(spec_dir)

    assert artifacts.specification_present is False
    assert artifacts.clarification_present is False
    assert artifacts.plan_present is False
    assert artifacts.tasks_present is False


def test_clarification_absent_when_section_heading_missing_entirely(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs" / "001-feature"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text(SPEC_WITH_NO_CLARIFICATIONS_SECTION_AT_ALL, encoding="utf-8")

    artifacts = discover_artifacts(spec_dir)

    assert artifacts.specification_present is True
    assert artifacts.clarification_present is False


def test_lookup_task_titles_for_a_range_within_the_file(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.md"
    tasks_path.write_text(TASKS_MD, encoding="utf-8")

    titles = lookup_task_titles(tasks_path, "T001", "T003")

    assert titles == {
        "T001": "Do the first thing",
        "T002": "Do the second thing",
        "T003": "Do the third thing",
    }


def test_lookup_task_titles_for_a_single_task_id(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.md"
    tasks_path.write_text(TASKS_MD, encoding="utf-8")

    assert lookup_task_titles(tasks_path, "T002", "T002") == {"T002": "Do the second thing"}


def test_lookup_task_titles_includes_an_id_outside_the_files_range_as_none(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.md"
    tasks_path.write_text(TASKS_MD, encoding="utf-8")

    titles = lookup_task_titles(tasks_path, "T002", "T005")

    assert titles["T002"] == "Do the second thing"
    assert titles["T003"] == "Do the third thing"
    assert titles["T004"] is None  # not in the file at all
    assert titles["T005"] is None


def test_lookup_task_titles_against_a_missing_tasks_file_returns_all_none(tmp_path: Path) -> None:
    tasks_path = tmp_path / "does-not-exist.md"

    titles = lookup_task_titles(tasks_path, "T001", "T002")

    assert titles == {"T001": None, "T002": None}
