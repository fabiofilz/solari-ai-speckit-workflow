"""Unit tests for `runs/audit.py` (T017)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from solari_workflow.runs.audit import (
    AuditTrailError,
    ResultAlreadyRecordedError,
    _next_sequence,
    _today_utc,
    create_run_record,
    render_metadata_header,
    write_result,
)


def test_create_run_record_writes_prompt_file_with_expected_stem(tmp_path: Path) -> None:
    record = create_run_record(tmp_path, slug_source="Validation Pipeline", prompt_content="hello")
    assert record.prompt_path.is_file()
    assert record.prompt_path.read_text(encoding="utf-8") == "hello"
    today = _today_utc()
    assert record.stem == f"{today}-001-validation-pipeline"
    assert record.prompt_path.name == f"{record.stem}-prompt.md"
    assert record.result_path.name == f"{record.stem}-result.md"


def test_sequence_numbering_increments_across_multiple_same_day_records(tmp_path: Path) -> None:
    first = create_run_record(tmp_path, slug_source="block one", prompt_content="p1")
    second = create_run_record(tmp_path, slug_source="block two", prompt_content="p2")
    third = create_run_record(tmp_path, slug_source="block three", prompt_content="p3")

    today = _today_utc()
    assert first.stem == f"{today}-001-block-one"
    assert second.stem == f"{today}-002-block-two"
    assert third.stem == f"{today}-003-block-three"


def test_next_sequence_ignores_files_from_a_different_date(tmp_path: Path) -> None:
    (tmp_path / "20200101-005-old-slug-prompt.md").write_text("x", encoding="utf-8")
    today = _today_utc()
    assert _next_sequence(tmp_path, today) == 1


def test_write_result_creates_the_paired_result_file(tmp_path: Path) -> None:
    record = create_run_record(tmp_path, slug_source="gate", prompt_content="prompt body")
    result_path = write_result(record, "RESULT: PASS\n")
    assert result_path == record.result_path
    assert result_path.read_text(encoding="utf-8") == "RESULT: PASS\n"


def test_write_result_never_overwrites_an_existing_result(tmp_path: Path) -> None:
    record = create_run_record(tmp_path, slug_source="gate", prompt_content="prompt body")
    write_result(record, "RESULT: PASS\n")

    with pytest.raises(ResultAlreadyRecordedError):
        write_result(record, "RESULT: FAIL\n")

    # Original content must survive the rejected "edit" attempt untouched.
    assert record.result_path.read_text(encoding="utf-8") == "RESULT: PASS\n"


def test_forced_naming_collision_is_resolved_by_retry_without_overwriting(tmp_path: Path) -> None:
    today = _today_utc()
    # Manually create the file the next sequence number would have used.
    colliding_path = tmp_path / f"{today}-001-manual-collision-prompt.md"
    tmp_path.mkdir(parents=True, exist_ok=True)
    os.makedirs(tmp_path, exist_ok=True)
    colliding_path.write_text("pre-existing content", encoding="utf-8")

    record = create_run_record(tmp_path, slug_source="manual collision", prompt_content="new content")

    # The pre-existing file must be untouched...
    assert colliding_path.read_text(encoding="utf-8") == "pre-existing content"
    # ...and the new record landed at the next available sequence number.
    assert record.stem == f"{today}-002-manual-collision"
    assert record.prompt_path.read_text(encoding="utf-8") == "new content"


def test_create_run_record_gives_up_after_bounded_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import solari_workflow.runs.audit as audit_module

    monkeypatch.setattr(audit_module, "_MAX_COLLISION_RETRIES", 3)
    monkeypatch.setattr(audit_module, "_next_sequence", lambda *_: 1)

    def _always_collide(path: Path, content: str) -> None:
        raise FileExistsError(str(path))

    monkeypatch.setattr(audit_module, "_create_exclusive", _always_collide)

    with pytest.raises(AuditTrailError):
        create_run_record(tmp_path, slug_source="doomed", prompt_content="x")


def test_slug_derivation_matches_shared_normalization_routine(tmp_path: Path) -> None:
    from solari_workflow.git.branch import to_slug

    record = create_run_record(tmp_path, slug_source="Café Résumé!!", prompt_content="x")
    expected_slug = to_slug("Café Résumé!!")
    assert expected_slug in record.stem
    assert re.match(rf"^\d{{8}}-\d{{3}}-{re.escape(expected_slug)}$", record.stem)


def test_render_metadata_header_includes_required_fields() -> None:
    header = render_metadata_header(
        tool="claude",
        model="opus",
        effort="high",
        session_mode="NEW",
        prior_session_id=None,
        speckit_involved=True,
        scope="T001-T003",
        purpose="implement block",
        workflow_schema_version="1",
        workflow_version="1.0.0",
    )
    assert "- Tool: claude" in header
    assert "- Model: opus" in header
    assert "- Effort: high" in header
    assert "- Session Mode: NEW" in header
    assert "- Spec Kit Involved: True" in header
    assert "- Scope: T001-T003" in header
    assert "- Purpose: implement block" in header
    assert "- Workflow Schema Version: 1" in header
    assert "- Workflow Version: 1.0.0" in header


def test_render_metadata_header_includes_prior_session_id_only_for_continue() -> None:
    header = render_metadata_header(
        tool="claude",
        model="opus",
        effort="high",
        session_mode="CONTINUE",
        prior_session_id="abc123",
        speckit_involved=False,
        scope="T001",
        purpose="remediate",
        workflow_schema_version="1",
        workflow_version="1.0.0",
    )
    assert "prior session: abc123" in header


def test_render_metadata_header_omits_prior_session_id_for_new_session() -> None:
    header = render_metadata_header(
        tool="claude",
        model="opus",
        effort="high",
        session_mode="NEW",
        prior_session_id=None,
        speckit_involved=False,
        scope="T001",
        purpose="implement",
        workflow_schema_version="1",
        workflow_version="1.0.0",
    )
    assert "prior session" not in header


def test_render_metadata_header_omits_paired_prompt_line_when_not_given() -> None:
    """A prompt file's own header has nothing to point to."""
    header = render_metadata_header(
        tool="claude",
        model="opus",
        effort="high",
        session_mode="NEW",
        prior_session_id=None,
        speckit_involved=False,
        scope="T001",
        purpose="implement",
        workflow_schema_version="1",
        workflow_version="1.0.0",
    )
    assert "Paired Prompt" not in header


def test_render_metadata_header_includes_explicit_paired_prompt_pointer_for_results() -> None:
    """research.md §8: a result file's header MUST carry "an explicit
    pointer back to its paired prompt file by shared `YYYYMMDD-NNN-slug`
    stem"."""
    header = render_metadata_header(
        tool="claude",
        model="opus",
        effort="high",
        session_mode="NEW",
        prior_session_id=None,
        speckit_involved=False,
        scope="T001",
        purpose="implement",
        workflow_schema_version="1",
        workflow_version="1.0.0",
        paired_prompt_stem="20260101-001-implement",
    )
    assert "- Paired Prompt: 20260101-001-implement-prompt.md" in header


def test_result_record_header_pairs_with_its_actual_prompt_file(tmp_path: Path) -> None:
    """End-to-end: the stem recorded in the result header must match the
    real prompt file `create_run_record` produced for the same record."""
    record = create_run_record(tmp_path, slug_source="gate", prompt_content="prompt body")
    result_header = render_metadata_header(
        tool="codex",
        model=None,
        effort=None,
        session_mode=None,
        prior_session_id=None,
        speckit_involved=True,
        scope="T001",
        purpose="gate",
        workflow_schema_version="1",
        workflow_version="1.0.0",
        paired_prompt_stem=record.stem,
    )
    write_result(record, result_header)

    assert f"- Paired Prompt: {record.prompt_path.name}" in result_header
    assert record.result_path.read_text(encoding="utf-8") == result_header
    # Immutability: the paired prompt file itself is untouched by writing the result.
    assert record.prompt_path.read_text(encoding="utf-8") == "prompt body"
