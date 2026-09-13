"""Unit tests for the `cli.py` `main()` error/exit boundary (T020 remediation).

Complements `tests/integration/test_cli_skeleton.py`'s real-subprocess
coverage with in-process tests that can monkeypatch internals to force
specific failure classes through `main()`.
"""

from __future__ import annotations

import pytest

from solari_workflow import cli
from solari_workflow.errors import EXIT_MANUAL_INTERVENTION, EXIT_NEGATIVE_RESULT, EXIT_SUCCESS


def test_main_maps_an_unexpected_exception_to_manual_intervention_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exception with no `WorkflowError` wrapper anywhere in the codebase
    (a genuinely unanticipated bug) must still exit `2`, never the bare
    `1` Python would otherwise assign an uncaught `Exception`."""

    def _boom(_config_path: object) -> None:
        raise ValueError("totally unexpected internal bug")

    monkeypatch.setattr(cli, "load_config", _boom)

    exit_code = cli.main(["readiness-check"])

    assert exit_code == EXIT_MANUAL_INTERVENTION
    assert exit_code not in (EXIT_SUCCESS, EXIT_NEGATIVE_RESULT)


def test_main_does_not_leak_a_raw_traceback_for_an_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(_config_path: object) -> None:
        raise KeyError("some_internal_key")

    monkeypatch.setattr(cli, "load_config", _boom)

    cli.main(["readiness-check"])

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "error:" in captured.err


def test_main_still_lets_a_workflow_error_use_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solari_workflow.errors import ConfigValidationError

    def _boom(_config_path: object) -> None:
        raise ConfigValidationError("bad config")

    monkeypatch.setattr(cli, "load_config", _boom)

    exit_code = cli.main(["readiness-check"])
    assert exit_code == EXIT_MANUAL_INTERVENTION


def test_main_does_not_catch_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI boundary's catch-all is for `Exception`, not `BaseException`
    — an operator's Ctrl+C must still actually interrupt the process."""

    def _boom(_config_path: object) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "load_config", _boom)

    with pytest.raises(KeyboardInterrupt):
        cli.main(["readiness-check"])
