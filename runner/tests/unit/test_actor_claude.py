"""Unit tests for `actors/claude.py` (T034), driven against the fake
`claude` CLI stub (`tests/fixtures/fake_claude.py`, T026) via an
executable-path override - never a real, paid model call.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from solari_workflow.actors import claude
from solari_workflow.errors import UnsupportedCLICapabilityError
from solari_workflow.platform import proc

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FAKE_CLAUDE = FIXTURES_DIR / "fake_claude.py"
ARGV_PREFIX = [sys.executable, str(FAKE_CLAUDE)]


def _run_session(tmp_path: Path, env: dict[str, str] | None = None, **overrides) -> claude.ClaudeSessionOutcome:
    full_env = {**os.environ, **env} if env is not None else None
    kwargs = dict(
        model="claude-test",
        effort="high",
        session_mode="NEW",
        session_id=None,
        prompt_body="implement the thing",
        cwd=str(tmp_path),
        argv_prefix=ARGV_PREFIX,
        env=full_env,
    )
    kwargs.update(overrides)
    return claude.run_claude_session(**kwargs)


def test_status_complete_is_parsed(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CLAUDE_STATUS": "COMPLETE"})
    assert outcome.status == "COMPLETE"


def test_status_architectural_decision_required_is_parsed(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CLAUDE_STATUS": "ARCHITECTURAL_DECISION_REQUIRED"})
    assert outcome.status == "ARCHITECTURAL_DECISION_REQUIRED"


def test_status_blocked_is_parsed(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CLAUDE_STATUS": "BLOCKED"})
    assert outcome.status == "BLOCKED"


def test_missing_marker_fails_closed_to_blocked(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CLAUDE_STATUS": "MISSING"})
    assert outcome.status == "BLOCKED"


def test_malformed_marker_fails_closed_to_blocked(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CLAUDE_STATUS": "MALFORMED"})
    assert outcome.status == "BLOCKED"


def test_transient_failure_retries_once_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake stub can't "recover" itself across two subprocess launches,
    so this proves the RETRY MECHANICS fire exactly once by making the
    underlying `run_streaming` call transient-fail on the first attempt
    and succeed on the second."""
    calls = {"count": 0}
    original = proc.run_streaming

    def _flaky(argv, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return proc.ProcessResult(returncode=17, stdout="", stderr="simulated", duration=0.0)
        return original(argv, **kwargs)

    monkeypatch.setattr(proc, "run_streaming", _flaky)
    outcome = _run_session(tmp_path, env={"FAKE_CLAUDE_STATUS": "COMPLETE"})
    assert calls["count"] == 2
    assert outcome.status == "COMPLETE"


def test_transient_failure_recurring_propagates_after_single_retry(tmp_path: Path) -> None:
    with pytest.raises(proc.TransientOperationalError):
        _run_session(tmp_path, env={"FAKE_CLAUDE_TRANSIENT_FAILURE": "1"})


def test_capability_check_rejects_when_a_required_flag_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        proc,
        "run",
        lambda *a, **k: proc.ProcessResult(returncode=0, stdout="Usage: claude [options]\n", stderr="", duration=0.0),
    )
    with pytest.raises(UnsupportedCLICapabilityError):
        _run_session(tmp_path)


def test_capability_check_can_be_skipped(tmp_path: Path) -> None:
    outcome = _run_session(tmp_path, env={"FAKE_CLAUDE_STATUS": "COMPLETE"}, verify_capabilities=False)
    assert outcome.status == "COMPLETE"


def test_continue_without_session_id_is_rejected() -> None:
    with pytest.raises(claude.ClaudeInvocationError):
        claude._build_claude_argv(model="m", effort="high", session_mode="CONTINUE", session_id=None)


def test_continue_with_session_id_uses_resume_flag() -> None:
    argv = claude._build_claude_argv(model="m", effort="high", session_mode="CONTINUE", session_id="abc-123")
    assert "--resume" in argv
    assert "abc-123" in argv


def test_build_argv_never_allows_git_bash_tool() -> None:
    argv = claude._build_claude_argv(model="m", effort="high", session_mode="NEW", session_id=None)
    idx = argv.index("--disallowedTools")
    assert "git" in argv[idx + 1]


def test_missing_model_is_rejected() -> None:
    with pytest.raises(claude.ClaudeInvocationError):
        claude._build_claude_argv(model="", effort="high", session_mode="NEW", session_id=None)


# --- B2 remediation: process-level success is a prerequisite to parsing ---


def test_nonzero_exit_forces_blocked_even_with_well_formed_complete_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_run_streaming(argv, **kwargs):  # noqa: ARG001 - test double
        return proc.ProcessResult(returncode=1, stdout="STATUS: COMPLETE\n", stderr="boom", duration=0.0)

    monkeypatch.setattr(proc, "run_streaming", _fake_run_streaming)
    outcome = _run_session(tmp_path, verify_capabilities=False)
    assert outcome.status == "BLOCKED"


def test_nonzero_exit_forces_blocked_even_with_well_formed_architectural_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_run_streaming(argv, **kwargs):  # noqa: ARG001 - test double
        return proc.ProcessResult(
            returncode=1, stdout="STATUS: ARCHITECTURAL_DECISION_REQUIRED\n", stderr="boom", duration=0.0
        )

    monkeypatch.setattr(proc, "run_streaming", _fake_run_streaming)
    outcome = _run_session(tmp_path, verify_capabilities=False)
    assert outcome.status == "BLOCKED"


def test_zero_exit_with_well_formed_marker_is_still_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check: the B2 fix does not break the ordinary success path."""

    def _fake_run_streaming(argv, **kwargs):  # noqa: ARG001 - test double
        return proc.ProcessResult(returncode=0, stdout="STATUS: COMPLETE\n", stderr="", duration=0.0)

    monkeypatch.setattr(proc, "run_streaming", _fake_run_streaming)
    outcome = _run_session(tmp_path, verify_capabilities=False)
    assert outcome.status == "COMPLETE"


# --- B2 remediation: no "last marker wins" - ambiguity fails closed -------


def test_parse_status_rejects_multiple_status_lines_even_if_consistent() -> None:
    assert claude._parse_status("STATUS: COMPLETE\nSTATUS: COMPLETE\n") == "BLOCKED"


def test_parse_status_rejects_multiple_conflicting_status_lines() -> None:
    assert claude._parse_status("STATUS: BLOCKED\nsome narrative text\nSTATUS: COMPLETE\n") == "BLOCKED"


def test_parse_status_rejects_a_marker_line_embedded_earlier_as_illustration() -> None:
    """An illustrative example of the contract shape earlier in the
    transcript, on its own line, must not be confused with the
    authoritative trailing marker - and per B2, its mere PRESENCE (not
    just its position) makes the whole output ambiguous, even though the
    true trailing marker below it is well-formed on its own."""
    stdout = (
        "Note: my final line will look like this example:\n"
        "STATUS: COMPLETE\n"
        "(that line above is just an example of the format)\n"
        "Now here is my real status:\n"
        "STATUS: COMPLETE\n"
    )
    assert claude._parse_status(stdout) == "BLOCKED"


def test_parse_status_rejects_content_after_the_marker() -> None:
    assert claude._parse_status("STATUS: COMPLETE\noops, one more line\n") == "BLOCKED"


def test_parse_status_accepts_a_single_well_formed_trailing_marker() -> None:
    assert claude._parse_status("doing work\nmore work\nSTATUS: COMPLETE\n") == "COMPLETE"


def test_parse_status_accepts_trailing_blank_lines_after_the_marker() -> None:
    assert claude._parse_status("STATUS: COMPLETE\n\n\n") == "COMPLETE"


# --- m1 remediation: capability verification covers every used flag -------


def test_capability_check_requires_disallowed_tools_flag() -> None:
    help_text = "Usage: claude\n--print\n--model\n--effort\n--append-system-prompt\n"
    with pytest.raises(UnsupportedCLICapabilityError) as excinfo:
        claude.verify_claude_capabilities(help_text, session_mode="NEW")
    assert "disallowedTools" in str(excinfo.value)


def test_capability_check_requires_append_system_prompt_flag() -> None:
    help_text = "Usage: claude\n--print\n--model\n--effort\n--disallowedTools\n"
    with pytest.raises(UnsupportedCLICapabilityError) as excinfo:
        claude.verify_claude_capabilities(help_text, session_mode="NEW")
    assert "append-system-prompt" in str(excinfo.value)


def test_build_argv_includes_a_broadened_wrapped_git_pattern() -> None:
    """M5: defense in depth - a second, broader pattern catches a
    wrapped/prefixed git invocation, not merely a literal `git ...`."""
    argv = claude._build_claude_argv(model="m", effort="high", session_mode="NEW", session_id=None)
    idx = argv.index("--disallowedTools")
    patterns = argv[idx + 1 : idx + 3]
    assert "Bash(git *)" in patterns
    assert "Bash(*git*)" in patterns


# --- item 2 / B2: strict column-0 protocol, adversarial cases --------------


def test_indented_example_marker_does_not_authorize_anything() -> None:
    """The gate's own literal example: 'Example:\\n  STATUS: COMPLETE' -
    the marker is indented, so it never even counts as a candidate."""
    stdout = "Example:\n  STATUS: COMPLETE\n"
    assert claude._parse_status(stdout) == "BLOCKED"


def test_marker_inside_a_fenced_code_block_does_not_authorize_anything() -> None:
    stdout = "Here is an example of the format:\n```\nSTATUS: COMPLETE\n```\n"
    assert claude._parse_status(stdout) == "BLOCKED"


def test_marker_inside_a_blockquote_does_not_authorize_anything() -> None:
    stdout = "> STATUS: COMPLETE\n"
    assert claude._parse_status(stdout) == "BLOCKED"


def test_unfenced_trailing_marker_after_a_fenced_example_is_still_accepted() -> None:
    """The fenced example must not count as a candidate, but a REAL,
    unfenced, trailing marker afterward must still be recognized -
    fencing an illustration must not accidentally poison the real
    answer that follows it."""
    stdout = "For reference:\n```\nSTATUS: COMPLETE\n```\nSTATUS: BLOCKED\n"
    assert claude._parse_status(stdout) == "BLOCKED"


def test_column_zero_trailing_marker_alone_is_accepted() -> None:
    assert claude._parse_status("some work\nSTATUS: COMPLETE\n") == "COMPLETE"


# --- Third remediation M2: tilde fences and fence grammar ------------------


def test_marker_inside_a_closed_tilde_fence_does_not_authorize_anything() -> None:
    assert claude._parse_status("Example:\n~~~\nSTATUS: COMPLETE\n~~~\n") == "BLOCKED"


def test_marker_inside_an_unclosed_tilde_fence_through_eof_does_not_authorize_anything() -> None:
    assert claude._parse_status("Example:\n~~~text\nSTATUS: COMPLETE\n") == "BLOCKED"


def test_marker_inside_an_unclosed_backtick_fence_through_eof_does_not_authorize_anything() -> None:
    assert claude._parse_status("Example:\n```\nSTATUS: COMPLETE\n") == "BLOCKED"


def test_real_marker_after_a_closed_tilde_fence_is_accepted() -> None:
    assert claude._parse_status("Example:\n~~~\nSTATUS: BLOCKED\n~~~\nSTATUS: COMPLETE\n") == "COMPLETE"


def test_mixed_backtick_and_tilde_fences_do_not_close_each_other() -> None:
    # A ``` line inside a ~~~ fence does not close it, so the marker is
    # still fenced (and the fence runs through EOF).
    assert claude._parse_status("~~~\n```\nSTATUS: COMPLETE\n") == "BLOCKED"
    assert claude._parse_status("```\n~~~\nSTATUS: COMPLETE\n~~~\n") == "BLOCKED"
    # A shorter closing run does not close a longer opener.
    assert claude._parse_status("~~~~\n~~~\nSTATUS: COMPLETE\n") == "BLOCKED"
    # Properly closed mixed fences followed by a real marker.
    assert claude._parse_status("```\nSTATUS: BLOCKED\n```\n~~~\nSTATUS: BLOCKED\n~~~\nSTATUS: COMPLETE\n") == "COMPLETE"


def test_quoted_and_indented_fenced_examples_do_not_authorize_anything() -> None:
    assert claude._parse_status("> ~~~\n> STATUS: COMPLETE\n> ~~~\n") == "BLOCKED"
    assert claude._parse_status("  ~~~\nSTATUS: COMPLETE\n  ~~~\n") == "BLOCKED"
    assert claude._parse_status("   ```\nSTATUS: COMPLETE\n") == "BLOCKED"


# --- Fourth remediation B1: only structurally valid closers close a fence --


@pytest.mark.parametrize(
    "stdout",
    [
        # blockquoted apparent closer does not close an unquoted fence
        "```\n> ```\nSTATUS: COMPLETE\n",
        "~~~\n> ~~~\nSTATUS: COMPLETE\n",
        # four-space-indented apparent closer does not close
        "```\n    ```\nSTATUS: COMPLETE\n",
        "~~~\n    ~~~\nSTATUS: COMPLETE\n",
        # tab-indented apparent closer does not close
        "```\n\t```\nSTATUS: COMPLETE\n",
        # unclosed outer fence containing quoted and indented apparent closers
        "```\n> ```\n    ```\n>     ```\nSTATUS: COMPLETE\n",
        # shorter run, other family, trailing text: none close
        "````\n```\nSTATUS: COMPLETE\n",
        "~~~~\n```\n~~~\nSTATUS: COMPLETE\n",
        "```\n``` not a closer\nSTATUS: COMPLETE\n",
        # quoted fence is not closed by an unquoted closer or by leaving the quote
        "> ```\n```\nSTATUS: COMPLETE\n",
        "> ~~~\nSTATUS: COMPLETE\n",
        # nested quote depth mismatch
        "> > ```\n> ```\nSTATUS: COMPLETE\n",
    ],
)
def test_apparent_closers_never_expose_a_later_marker(stdout: str) -> None:
    assert claude._parse_status(stdout) == "BLOCKED"


@pytest.mark.parametrize(
    "stdout",
    [
        "```\nSTATUS: BLOCKED\n```\nSTATUS: COMPLETE\n",
        "~~~\nSTATUS: BLOCKED\n   ~~~\nSTATUS: COMPLETE\n",  # 3-space closer is valid
        "````\nSTATUS: BLOCKED\n`````  \nSTATUS: COMPLETE\n",  # longer closer, trailing spaces
        "> ```\n> STATUS: BLOCKED\n> ```\nSTATUS: COMPLETE\n",  # closer at same quote depth
        "  ~~~ text\n> ~~~\n    ~~~\nSTATUS: BLOCKED\n ~~~\nSTATUS: COMPLETE\n",
    ],
)
def test_real_marker_after_a_legitimate_closer_is_accepted(stdout: str) -> None:
    assert claude._parse_status(stdout) == "COMPLETE"


def test_four_space_indented_fence_is_not_an_opener_that_could_pair_with_a_real_opener() -> None:
    # "    ```" is indented code, not an opener; the next ``` opens a fence
    # that hides the marker, so nothing is exposed by mis-pairing.
    assert claude._parse_status("    ```\n```\nSTATUS: COMPLETE\n") == "BLOCKED"


def test_inline_backticks_are_not_a_fence_opener() -> None:
    assert claude._parse_status("```inline``` code\nSTATUS: COMPLETE\n") == "COMPLETE"
