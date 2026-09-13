"""Unit tests for the shared exception types and exit-code mapping (T004)."""

from solari_workflow.errors import (
    EXIT_MANUAL_INTERVENTION,
    EXIT_NEGATIVE_RESULT,
    EXIT_SUCCESS,
    ConfigValidationError,
    GitAmbiguityError,
    StagingPreconditionError,
    UnsupportedCLICapabilityError,
    WorkflowError,
    exit_code_for,
)


def test_exit_code_constants_match_cli_contract() -> None:
    assert EXIT_SUCCESS == 0
    assert EXIT_NEGATIVE_RESULT == 1
    assert EXIT_MANUAL_INTERVENTION == 2


def test_every_workflow_error_defaults_to_manual_intervention_exit_code() -> None:
    for cls in (
        WorkflowError,
        ConfigValidationError,
        GitAmbiguityError,
        StagingPreconditionError,
        UnsupportedCLICapabilityError,
    ):
        instance = cls("boom")
        assert instance.exit_code == EXIT_MANUAL_INTERVENTION
        assert exit_code_for(instance) == EXIT_MANUAL_INTERVENTION


def test_staging_precondition_error_is_a_git_ambiguity_error() -> None:
    assert issubclass(StagingPreconditionError, GitAmbiguityError)


def test_config_validation_error_carries_aggregated_problems() -> None:
    problems = [
        ("environment.lockfile", "missing required field"),
        ("git.push.mode", "invalid value 'auto'"),
    ]
    error = ConfigValidationError("2 problems found", problems=problems)
    assert error.problems == problems
    assert error.message == "2 problems found"


def test_config_validation_error_defaults_to_empty_problem_list() -> None:
    error = ConfigValidationError("no details")
    assert error.problems == []


def test_staging_precondition_error_carries_staged_paths() -> None:
    error = StagingPreconditionError("blocked", staged_paths=["a.txt", "b.txt"])
    assert error.staged_paths == ["a.txt", "b.txt"]


def test_exit_code_for_non_workflow_error_falls_back_to_manual_intervention() -> None:
    assert exit_code_for(RuntimeError("unexpected")) == EXIT_MANUAL_INTERVENTION


def test_exit_code_for_success_is_never_returned_for_an_exception() -> None:
    # An exception, by definition, never represents the success path — this
    # guards against a future WorkflowError subclass accidentally claiming
    # exit code 0.
    for cls in (WorkflowError, ConfigValidationError, GitAmbiguityError):
        assert exit_code_for(cls("x")) != EXIT_SUCCESS
