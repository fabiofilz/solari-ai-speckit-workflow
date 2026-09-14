"""Unit tests for `actors/codex.py` (T036) - both the `RESULT:`/`FINDINGS:`
parser (`contracts/codex-gate-result-contract.md`) in isolation, and the
full session flow driven against the fake `codex` CLI stub
(`tests/fixtures/fake_codex.py`, T026) via an executable-path override.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from solari_workflow.actors import codex
from solari_workflow.errors import UnsupportedCLICapabilityError
from solari_workflow.platform import proc

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FAKE_CODEX = FIXTURES_DIR / "fake_codex.py"
ARGV_PREFIX = [sys.executable, str(FAKE_CODEX)]


def _run_session(tmp_path: Path, env: dict[str, str] | None = None, **overrides) -> codex.CodexSessionOutcome:
    full_env = {**os.environ, **env} if env is not None else None
    kwargs = dict(
        prompt_body="review the candidate tree",
        cwd=str(tmp_path),
        argv_prefix=ARGV_PREFIX,
        env=full_env,
    )
    kwargs.update(overrides)
    return codex.run_codex_gate_session(**kwargs)


# --- parse_codex_result: the deterministic contract parser --------------


def test_well_formed_pass_with_no_findings() -> None:
    result = codex.parse_codex_result("RESULT: PASS\nFINDINGS: []\n")
    assert result.parsed is True
    assert result.result == "PASS"
    assert result.findings == ()


def test_well_formed_fail_with_findings() -> None:
    stdout = "RESULT: FAIL\nFINDINGS:\n- severity: blocker\n  summary: missing null check\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is True
    assert result.result == "FAIL"
    assert result.findings == (codex.CodexFinding(severity="blocker", summary="missing null check", location=None),)


def test_finding_with_location() -> None:
    stdout = (
        "RESULT: FAIL\nFINDINGS:\n- severity: major\n  summary: bug\n  location: foo.py:12\n"
    )
    result = codex.parse_codex_result(stdout)
    assert result.findings[0].location == "foo.py:12"


def test_missing_trailing_block_is_unparseable() -> None:
    result = codex.parse_codex_result("just some prose with no marker at all\n")
    assert result.parsed is False
    assert result.result == "FAIL"


def test_finding_with_invalid_severity_makes_whole_result_unparseable() -> None:
    stdout = "RESULT: PASS\nFINDINGS:\n- severity: catastrophic\n  summary: x\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False
    assert result.result == "FAIL"


def test_finding_missing_severity_makes_whole_result_unparseable() -> None:
    stdout = "RESULT: PASS\nFINDINGS:\n- summary: x\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False


def test_case_sensitivity_pass_is_unparseable() -> None:
    result = codex.parse_codex_result("RESULT: Pass\nFINDINGS: []\n")
    assert result.parsed is False
    assert result.result == "FAIL"


def test_empty_findings_list_accepted_as_no_findings() -> None:
    result = codex.parse_codex_result("RESULT: PASS\nFINDINGS:\n")
    assert result.parsed is True
    assert result.findings == ()


def test_only_the_trailing_block_is_considered() -> None:
    """A `RESULT:`-looking line elsewhere in free-form prose must not be
    mistaken for the authoritative trailing block."""
    stdout = "Some notes mention RESULT: PASS in passing.\n\nRESULT: FAIL\nFINDINGS: []\n"
    result = codex.parse_codex_result(stdout)
    assert result.result == "FAIL"


# --- is_checkpoint_eligible ------------------------------------------------


def test_eligible_pass_with_no_blocking_findings() -> None:
    result = codex.CodexGateResult(
        result="PASS", findings=(codex.CodexFinding(severity="minor", summary="x"),), parsed=True
    )
    assert codex.is_checkpoint_eligible(result, ["blocker", "major"]) is True


def test_pass_with_blocking_finding_is_not_eligible() -> None:
    result = codex.CodexGateResult(
        result="PASS", findings=(codex.CodexFinding(severity="blocker", summary="x"),), parsed=True
    )
    assert codex.is_checkpoint_eligible(result, ["blocker", "major"]) is False


def test_fail_is_never_eligible() -> None:
    result = codex.CodexGateResult(result="FAIL", findings=(), parsed=True)
    assert codex.is_checkpoint_eligible(result, ["blocker", "major"]) is False


def test_unparsed_is_never_eligible() -> None:
    result = codex.CodexGateResult(result="FAIL", findings=(), parsed=False)
    assert codex.is_checkpoint_eligible(result, ["blocker", "major"]) is False


# --- run_codex_gate_session: full flow against the fake stub ---------------


def test_pass_with_no_findings_end_to_end(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CODEX_RESULT": "PASS"})
    assert outcome.parsed.result == "PASS"
    assert outcome.parsed.parsed is True


def test_fail_end_to_end(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CODEX_RESULT": "FAIL"})
    assert outcome.parsed.result == "FAIL"


def test_missing_block_end_to_end_fails_closed(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CODEX_RESULT": "MISSING"})
    assert outcome.parsed.parsed is False
    assert outcome.parsed.result == "FAIL"


def test_transient_failure_recurring_propagates_after_single_retry(tmp_path: Path) -> None:
    with pytest.raises(proc.TransientOperationalError):
        _run_session(tmp_path, env={"FAKE_CODEX_TRANSIENT_FAILURE": "1"})


def test_transient_failure_retries_once_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}
    original = proc.run_streaming

    def _flaky(argv, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return proc.ProcessResult(returncode=17, stdout="", stderr="simulated", duration=0.0)
        return original(argv, **kwargs)

    monkeypatch.setattr(proc, "run_streaming", _flaky)
    outcome = _run_session(tmp_path, env={"FAKE_CODEX_RESULT": "PASS"})
    assert calls["count"] == 2
    assert outcome.parsed.result == "PASS"


def test_capability_check_rejects_when_a_required_flag_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        proc,
        "run",
        lambda *a, **k: proc.ProcessResult(returncode=0, stdout="Usage: codex [OPTIONS]\n", stderr="", duration=0.0),
    )
    with pytest.raises(UnsupportedCLICapabilityError):
        _run_session(tmp_path)


def test_capability_check_can_be_skipped(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CODEX_RESULT": "PASS"}, verify_capabilities=False)
    assert outcome.parsed.result == "PASS"


def test_build_argv_uses_read_only_sandbox() -> None:
    argv = codex._build_codex_argv()
    assert "--sandbox" in argv
    assert "read-only" in argv


# --- B2 remediation: process-level success is a prerequisite to parsing ---


def test_nonzero_exit_forces_fail_even_with_well_formed_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_run_streaming(argv, **kwargs):  # noqa: ARG001 - test double
        return proc.ProcessResult(returncode=1, stdout="RESULT: PASS\nFINDINGS: []\n", stderr="boom", duration=0.0)

    monkeypatch.setattr(proc, "run_streaming", _fake_run_streaming)
    outcome = _run_session(tmp_path, verify_capabilities=False)
    assert outcome.parsed.result == "FAIL"
    assert outcome.parsed.parsed is False


def test_zero_exit_with_well_formed_pass_is_still_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_run_streaming(argv, **kwargs):  # noqa: ARG001 - test double
        return proc.ProcessResult(returncode=0, stdout="RESULT: PASS\nFINDINGS: []\n", stderr="", duration=0.0)

    monkeypatch.setattr(proc, "run_streaming", _fake_run_streaming)
    outcome = _run_session(tmp_path, verify_capabilities=False)
    assert outcome.parsed.result == "PASS"
    assert outcome.parsed.parsed is True


# --- B2 remediation: no "last marker wins" - ambiguity fails closed -------


def test_multiple_result_lines_are_ambiguous_even_if_consistent() -> None:
    result = codex.parse_codex_result("RESULT: PASS\nFINDINGS: []\nRESULT: PASS\nFINDINGS: []\n")
    assert result.parsed is False
    assert result.result == "FAIL"


def test_multiple_conflicting_result_lines_are_ambiguous() -> None:
    stdout = "RESULT: FAIL\nFINDINGS: []\nnarrative text\nRESULT: PASS\nFINDINGS: []\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False
    assert result.result == "FAIL"


def test_result_line_embedded_earlier_as_illustration_makes_it_ambiguous() -> None:
    stdout = (
        "Note: the required format is:\n"
        "RESULT: PASS\n"
        "FINDINGS: []\n"
        "(that block above is just an example)\n"
        "Now here is my real result:\n"
        "RESULT: PASS\n"
        "FINDINGS: []\n"
    )
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False
    assert result.result == "FAIL"


def test_content_after_the_findings_block_is_rejected() -> None:
    result = codex.parse_codex_result("RESULT: PASS\nFINDINGS: []\none more line that should not be here\n")
    assert result.parsed is False


# --- m1 remediation: capability verification covers every used flag -------


def test_capability_check_requires_exec_subcommand() -> None:
    help_text = "--sandbox\n-c\n"
    with pytest.raises(UnsupportedCLICapabilityError) as excinfo:
        codex.verify_codex_capabilities(help_text)
    assert "exec" in str(excinfo.value)


# --- item 2 / B2: strict column-0 protocol, adversarial cases --------------


def test_indented_example_result_does_not_authorize_anything() -> None:
    """The gate's own literal example: 'Example:\\n  RESULT: PASS' - the
    marker is indented, so it never even counts as a candidate."""
    stdout = "Example:\n  RESULT: PASS\n  FINDINGS: []\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False
    assert result.result == "FAIL"


def test_result_inside_a_fenced_code_block_does_not_authorize_anything() -> None:
    stdout = "Here is the expected format:\n```\nRESULT: PASS\nFINDINGS: []\n```\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False


def test_result_inside_a_blockquote_does_not_authorize_anything() -> None:
    stdout = "> RESULT: PASS\n> FINDINGS: []\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False


def test_unfenced_trailing_result_after_a_fenced_example_is_still_accepted() -> None:
    stdout = "For reference:\n```\nRESULT: PASS\nFINDINGS: []\n```\nRESULT: FAIL\nFINDINGS: []\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is True
    assert result.result == "FAIL"


def test_findings_header_must_also_start_at_column_zero() -> None:
    stdout = "RESULT: PASS\n  FINDINGS: []\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False


# --- item 2: every finding must satisfy the full formal schema ------------


def test_finding_missing_summary_field_makes_whole_result_unparseable() -> None:
    stdout = "RESULT: FAIL\nFINDINGS:\n- severity: blocker\n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False
    assert result.result == "FAIL"


def test_finding_with_empty_summary_field_makes_whole_result_unparseable() -> None:
    stdout = "RESULT: FAIL\nFINDINGS:\n- severity: blocker\n  summary: \n"
    result = codex.parse_codex_result(stdout)
    assert result.parsed is False


def test_pass_with_a_malformed_finding_is_never_eligible() -> None:
    """item 2: 'a PASS with invalid/malformed findings is not eligible' -
    satisfied because a malformed finding makes the WHOLE result
    unparseable (parsed=False, result=FAIL), which `is_checkpoint_eligible`
    already treats as never eligible."""
    stdout = "RESULT: PASS\nFINDINGS:\n- severity: blocker\n"
    result = codex.parse_codex_result(stdout)
    assert codex.is_checkpoint_eligible(result, ["blocker", "major"]) is False


# --- Third remediation M2: tilde fences and fence grammar ------------------


def test_result_inside_a_closed_tilde_fence_does_not_authorize_anything() -> None:
    assert codex.parse_codex_result("Format:\n~~~\nRESULT: PASS\nFINDINGS: []\n~~~\n").parsed is False


def test_result_inside_an_unclosed_tilde_fence_does_not_authorize_anything() -> None:
    assert codex.parse_codex_result("Format:\n~~~\nRESULT: PASS\nFINDINGS: []\n").parsed is False


def test_real_result_after_a_closed_tilde_fence_is_accepted() -> None:
    result = codex.parse_codex_result("Format:\n~~~\nRESULT: FAIL\nFINDINGS: []\n~~~\nRESULT: PASS\nFINDINGS: []\n")
    assert result.parsed is True
    assert result.result == "PASS"


def test_mixed_fences_do_not_close_each_other_for_codex() -> None:
    assert codex.parse_codex_result("~~~\n```\nRESULT: PASS\nFINDINGS: []\n```\n").parsed is False


def test_quoted_or_indented_fenced_result_does_not_authorize_anything() -> None:
    assert codex.parse_codex_result("> ~~~\n> RESULT: PASS\n> FINDINGS: []\n> ~~~\n").parsed is False
    assert codex.parse_codex_result("  ~~~\nRESULT: PASS\nFINDINGS: []\n").parsed is False


# --- Fourth remediation B1: only structurally valid closers close a fence --

_BLOCK = "RESULT: PASS\nFINDINGS: []\n"


@pytest.mark.parametrize(
    "prefix",
    [
        "```\n> ```\n",
        "~~~\n> ~~~\n",
        "```\n    ```\n",
        "~~~\n    ~~~\n",
        "```\n\t```\n",
        "```\n> ```\n    ```\n>     ```\n",
        "````\n```\n",
        "~~~~\n```\n~~~\n",
        "> ```\n```\n",
        "> ~~~\n",
        "> > ```\n> ```\n",
    ],
)
def test_apparent_closers_never_expose_a_later_result(prefix: str) -> None:
    result = codex.parse_codex_result(prefix + _BLOCK)
    assert result.parsed is False
    assert result.result == "FAIL"


@pytest.mark.parametrize(
    "prefix",
    [
        "```\nRESULT: FAIL\nFINDINGS: []\n```\n",
        "~~~\nRESULT: FAIL\n   ~~~\n",
        "````\n`````  \n",
        "> ~~~\n> RESULT: FAIL\n> ~~~\n",
    ],
)
def test_real_result_after_a_legitimate_closer_is_accepted(prefix: str) -> None:
    result = codex.parse_codex_result(prefix + _BLOCK)
    assert result.parsed is True
    assert result.result == "PASS"


def test_fenced_final_block_is_never_authoritative() -> None:
    """The contract is unfenced; a final block wrapped in a fence is FAIL."""
    assert codex.parse_codex_result("review done\n```\n" + _BLOCK + "```\n").parsed is False
    assert codex.parse_codex_result("review done\n~~~\n" + _BLOCK + "~~~\n").parsed is False


def test_contract_suffix_instructs_an_unfenced_block_and_that_shape_parses() -> None:
    suffix = codex._RESULT_CONTRACT_SUFFIX
    assert "UNFENCED" in suffix
    assert "with a fenced block" not in suffix
    assert codex.parse_codex_result("Review notes.\n\n" + _BLOCK).parsed is True
