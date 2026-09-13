"""Unit tests for `platform/proc.py` (T006)."""

from __future__ import annotations

import sys
import textwrap

import pytest

from solari_workflow.platform import proc


def test_resolve_executable_missing_raises_actionable_error() -> None:
    with pytest.raises(proc.ExecutableNotFoundError) as excinfo:
        proc.resolve_executable("definitely-not-a-real-executable-xyz")
    assert "not found on PATH" in str(excinfo.value)
    assert excinfo.value.executable == "definitely-not-a-real-executable-xyz"


def test_run_returns_nonzero_exit_as_data_by_default() -> None:
    result = proc.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.returncode == 3
    assert result.ok is False


def test_run_check_true_raises_command_failed_on_nonzero_exit() -> None:
    with pytest.raises(proc.CommandFailed) as excinfo:
        proc.run([sys.executable, "-c", "import sys; sys.exit(1)"], check=True)
    assert excinfo.value.result.returncode == 1


def test_run_check_true_does_not_raise_on_zero_exit() -> None:
    result = proc.run([sys.executable, "-c", "print('ok')"], check=True)
    assert result.ok
    assert "ok" in result.stdout


def test_run_captures_stdout_and_stderr_separately() -> None:
    script = "import sys; sys.stdout.write('out'); sys.stderr.write('err')"
    result = proc.run([sys.executable, "-c", script])
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_run_never_interprets_shell_metacharacters_from_argv() -> None:
    # A shell would split/interpret `;` or `$(...)`; list-argv must not.
    dangerous = "; echo injected; $(echo also-injected)"
    result = proc.run([sys.executable, "-c", "import sys; print(sys.argv[1])", dangerous])
    assert result.stdout.strip() == dangerous
    assert "injected" not in result.stdout.replace(dangerous, "")


def test_run_streaming_captures_multiple_lines_in_order() -> None:
    script = textwrap.dedent(
        """
        import sys
        for i in range(3):
            print(f"line-{i}")
            sys.stdout.flush()
        """
    )
    result = proc.run_streaming([sys.executable, "-c", script], echo=False)
    assert result.ok
    assert result.stdout == "line-0\nline-1\nline-2\n"


def test_run_streaming_captures_nonzero_exit() -> None:
    result = proc.run_streaming(
        [sys.executable, "-c", "import sys; sys.exit(7)"], echo=False
    )
    assert result.returncode == 7


def test_is_process_alive_true_for_current_process() -> None:
    import os

    assert proc.is_process_alive(os.getpid()) is True


def test_is_process_alive_false_for_a_pid_unlikely_to_exist() -> None:
    # A very large PID is (best-effort) unlikely to be assigned.
    assert proc.is_process_alive(2**30) in (False, None)


# --- Windows liveness decision logic, exercised via injected fakes (T053/finding #8) ---
#
# `_is_process_alive_windows` accepts an injectable `kernel32`/`get_last_error`
# specifically so this decision logic can be unit-tested on macOS/Linux
# without the real Win32 FFI — see its docstring in `platform/proc.py`.


class _FakeKernel32:
    """A `kernel32`-shaped double: no real ctypes FFI, just Python callables."""

    def __init__(self, *, open_process_handle, exit_code: int | None = None) -> None:
        self._open_process_handle = open_process_handle
        self._exit_code = exit_code
        self.closed_handles: list[int] = []

    def OpenProcess(self, desired_access: int, inherit_handle: bool, pid: int) -> int:
        return self._open_process_handle

    def GetExitCodeProcess(self, handle: int, exit_code_ptr) -> int:
        if self._exit_code is None:
            return 0  # simulate GetExitCodeProcess itself failing
        exit_code_ptr.contents.value = self._exit_code
        return 1

    def CloseHandle(self, handle: int) -> int:
        self.closed_handles.append(handle)
        return 1


def test_windows_liveness_true_for_a_still_active_process() -> None:
    kernel32 = _FakeKernel32(open_process_handle=42, exit_code=259)  # STILL_ACTIVE
    result = proc._is_process_alive_windows(1234, kernel32=kernel32, get_last_error=lambda: 0)
    assert result is True
    assert kernel32.closed_handles == [42]


def test_windows_liveness_false_for_an_exited_process_handle() -> None:
    kernel32 = _FakeKernel32(open_process_handle=42, exit_code=0)  # not STILL_ACTIVE
    result = proc._is_process_alive_windows(1234, kernel32=kernel32, get_last_error=lambda: 0)
    assert result is False
    assert kernel32.closed_handles == [42]


def test_windows_liveness_false_when_open_process_reports_invalid_parameter() -> None:
    """`OpenProcess` failing with `ERROR_INVALID_PARAMETER` (87) is
    Windows' own specific "no such process" result — a legitimate
    "not alive", the ONLY failure code confidently reported as `False`."""
    kernel32 = _FakeKernel32(open_process_handle=0)
    result = proc._is_process_alive_windows(1234, kernel32=kernel32, get_last_error=lambda: 87)
    assert result is False


def test_windows_liveness_unknown_not_false_when_open_process_is_access_denied() -> None:
    """Regression for the first Codex finding: `OpenProcess` failing with
    `ERROR_ACCESS_DENIED` means the process EXISTS (owned by another
    user/session) but cannot be queried — this must be reported as
    unknown (`None`), never confidently "not alive" (`False`)."""
    kernel32 = _FakeKernel32(open_process_handle=0)
    result = proc._is_process_alive_windows(1234, kernel32=kernel32, get_last_error=lambda: 5)
    assert result is None


@pytest.mark.parametrize("error_code", [8, 1450, 995, 6, 0, 123456])
def test_windows_liveness_unknown_for_any_other_unrecognized_failure_code(error_code: int) -> None:
    """Regression for the second-re-gate Codex finding: `OpenProcess`
    failing for ANY reason other than the specific `ERROR_INVALID_PARAMETER`
    "no such process" result — e.g. `ERROR_NOT_ENOUGH_MEMORY` (8),
    `ERROR_NO_SYSTEM_RESOURCES` (1450), `ERROR_OPERATION_ABORTED` (995),
    or any other/unrecognized error — must be reported as unknown
    (`None`), never confidently "not alive" (`False`). Only
    `ERROR_INVALID_PARAMETER` (87) and `ERROR_ACCESS_DENIED` (5, covered
    separately above) are given special meaning; everything else falls
    through to the same conservative "unknown" default."""
    kernel32 = _FakeKernel32(open_process_handle=0)
    result = proc._is_process_alive_windows(1234, kernel32=kernel32, get_last_error=lambda: error_code)
    assert result is None


def test_windows_liveness_unknown_when_get_exit_code_process_fails() -> None:
    kernel32 = _FakeKernel32(open_process_handle=42, exit_code=None)
    result = proc._is_process_alive_windows(1234, kernel32=kernel32, get_last_error=lambda: 0)
    assert result is None
    assert kernel32.closed_handles == [42]  # the handle is still closed on this path


def test_windows_liveness_unknown_never_raises_on_an_unexpected_kernel32_error() -> None:
    class _ExplodingKernel32:
        def OpenProcess(self, *_args: object) -> int:
            raise OSError("simulated FFI failure")

    result = proc._is_process_alive_windows(1234, kernel32=_ExplodingKernel32(), get_last_error=lambda: 0)
    assert result is None


def test_run_with_single_retry_succeeds_without_retry_when_first_call_ok() -> None:
    calls = []

    def operation() -> str:
        calls.append(1)
        return "ok"

    result = proc.run_with_single_retry(operation)
    assert result == "ok"
    assert len(calls) == 1


def test_run_with_single_retry_retries_exactly_once_then_succeeds() -> None:
    calls = []

    def operation() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise proc.TransientOperationalError("transient launch failure")
        return "ok-after-retry"

    result = proc.run_with_single_retry(operation)
    assert result == "ok-after-retry"
    assert len(calls) == 2


def test_run_with_single_retry_gives_up_after_one_retry() -> None:
    calls = []

    def operation() -> str:
        calls.append(1)
        raise proc.TransientOperationalError("still failing")

    with pytest.raises(proc.TransientOperationalError):
        proc.run_with_single_retry(operation)
    assert len(calls) == 2  # original attempt + exactly one retry, never more


def test_run_with_single_retry_does_not_retry_non_transient_errors() -> None:
    calls = []

    def operation() -> str:
        calls.append(1)
        raise ValueError("not transient")

    with pytest.raises(ValueError):
        proc.run_with_single_retry(operation)
    assert len(calls) == 1


def test_run_with_single_retry_invokes_on_retry_callback_with_attempt_number() -> None:
    seen = []

    def operation() -> str:
        if not seen:
            raise proc.TransientOperationalError("boom")
        return "ok"

    def on_retry(error: proc.TransientOperationalError, attempt: int) -> None:
        seen.append((str(error), attempt))

    result = proc.run_with_single_retry(operation, on_retry=on_retry)
    assert result == "ok"
    assert seen == [("boom", 1)]
