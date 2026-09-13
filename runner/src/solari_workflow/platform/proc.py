"""Cross-platform subprocess execution primitives (research.md §6).

Every module that shells out (`git`, `claude`, `codex`, `uv`, and
configured project validation commands) is expected to go through this
module rather than calling :mod:`subprocess` directly. Argv is always
list-form — ``shell=True`` and manual shell-string concatenation are
never used anywhere in this codebase, which is the standard defense
against injection from config-sourced or Spec Kit task-title strings.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO, Callable, Sequence, TypeVar

from solari_workflow.errors import WorkflowError

T = TypeVar("T")


class ExecutableNotFoundError(WorkflowError):
    """`shutil.which` could not resolve a required executable on PATH."""

    def __init__(self, executable: str) -> None:
        super().__init__(f"'{executable}' not found on PATH")
        self.executable = executable


@dataclass(frozen=True)
class ProcessResult:
    """The deterministic outcome of one subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandFailed(WorkflowError):
    """A subprocess exit is unambiguously a runner-internal error at this call site.

    Non-zero exit codes are *data*, not automatically exceptions — this
    is only raised when ``check=True`` was explicitly requested by a
    call site that has decided a non-zero exit here can only mean an
    operational failure (e.g. `git` itself failing during a checkpoint
    step), never a meaningful PASS/FAIL result to inspect.
    """

    def __init__(self, argv: Sequence[str], result: ProcessResult) -> None:
        message = (
            f"command failed (exit {result.returncode}): {' '.join(argv)}\n"
            f"stderr: {result.stderr.strip()}"
        )
        super().__init__(message)
        self.argv = list(argv)
        self.result = result


class TransientOperationalError(WorkflowError):
    """A plausibly transient, operational failure (research.md §0.6).

    Only this exception class triggers the single automatic retry
    (:func:`run_with_single_retry`) — every other exception, including
    every other :class:`~solari_workflow.errors.WorkflowError`, is a
    non-retryable failure class (invalid config, Git ambiguity, an
    architectural-decision stop, a persistent QA finding) and propagates
    immediately.
    """


def resolve_executable(name: str) -> str:
    """Resolve `name` to an absolute path via `shutil.which`, or fail closed.

    Resolving up front (rather than letting the OS shell resolve it)
    keeps the "not found" error message identical across macOS/Windows/
    Linux, per research.md §6.
    """
    path = shutil.which(name)
    if path is None:
        raise ExecutableNotFoundError(name)
    return path


def run(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> ProcessResult:
    """Run `argv` (never through a shell) and capture stdout/stderr/exit/duration.

    Output is decoded as UTF-8 text with ``errors="replace"`` so odd
    bytes from a subprocess never crash the runner. Non-zero exit is data
    by default (``check=False``) — callers decide whether it means
    PASS/FAIL. Pass ``check=True`` to instead raise :class:`CommandFailed`
    on a non-zero exit.
    """
    start = time.monotonic()
    completed = subprocess.run(  # noqa: S603 - list-argv only, never shell=True
        list(argv),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    duration = time.monotonic() - start
    result = ProcessResult(
        returncode=completed.returncode,
        stdout=completed.stdout.decode("utf-8", errors="replace"),
        stderr=completed.stderr.decode("utf-8", errors="replace"),
        duration=duration,
    )
    if check and not result.ok:
        raise CommandFailed(argv, result)
    return result


def run_streaming(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    echo: bool = True,
) -> ProcessResult:
    """Run a long-running session (`claude`/`codex`), streaming its output.

    stdout/stderr are read line-by-line on background threads (stdlib
    `threading`, no third-party process-streaming library) and each line
    is simultaneously (a) echoed to the operator's terminal when ``echo``
    is true, and (b) accumulated into the returned :class:`ProcessResult`
    for `.ai-runs/*-result.md` transcript persistence (research.md §6/§8).
    """
    start = time.monotonic()
    process = subprocess.Popen(  # noqa: S603 - list-argv only, never shell=True
        list(argv),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _pump(stream: IO[bytes], sink: list[str], echo_stream: IO[str]) -> None:
        for raw_line in iter(stream.readline, b""):
            line = raw_line.decode("utf-8", errors="replace")
            sink.append(line)
            if echo:
                echo_stream.write(line)
                echo_stream.flush()
        stream.close()

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(
        target=_pump, args=(process.stdout, stdout_lines, sys.stdout), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_pump, args=(process.stderr, stderr_lines, sys.stderr), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    duration = time.monotonic() - start

    return ProcessResult(
        returncode=returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        duration=duration,
    )


def is_process_alive(pid: int) -> bool | None:
    """Best-effort, non-destructive liveness check (research.md §7).

    Returns ``True``/``False`` when determinable, ``None`` when liveness
    cannot be determined on this platform — never raises. Used only to
    *annotate* a lock-contention message; the runner never acts on this
    result to auto-remove another process's lock.
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        return _is_process_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists (owned by another user) - still alive.
        return True
    except OSError:
        return None
    else:
        return True


_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN_STILL_ACTIVE = 259
_WIN_ERROR_ACCESS_DENIED = 5
_WIN_ERROR_INVALID_PARAMETER = 87


def _load_kernel32():  # pragma: no cover - exercised only on real Windows
    """Load `kernel32` with explicit `ctypes` signatures for every call used below.

    Explicit `argtypes`/`restype` matter beyond style here: `HANDLE` is
    pointer-sized (64 bits on Win64), and `ctypes`' default guessed
    return type for an undeclared function is a 32-bit `c_int` — on a
    64-bit process that silently truncates a real handle value. Declaring
    `OpenProcess`'s `restype` as `c_void_p` (pointer-width) avoids that
    class of corruption; `GetExitCodeProcess`/`CloseHandle` get explicit
    signatures for the same reason (correctness, not merely style).
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _is_process_alive_windows(pid: int, *, kernel32=None, get_last_error=None) -> bool | None:
    """Windows liveness decision logic, isolated from `ctypes.WinDLL` loading.

    `kernel32`/`get_last_error` are injectable purely so this decision
    logic (not the real Win32 FFI, which cannot run off Windows) can be
    unit-tested on macOS/Linux with a fake `kernel32`-shaped object — see
    `tests/unit/test_proc.py`.

    Returns ``True``/``False`` when determinable, ``None`` ("unknown")
    when it cannot be — never raises, and never treats "unknown" as
    "not alive". `OpenProcess` failing is classified conservatively —
    "confidently dead" is a narrow allowlist, not the default for every
    failure: only `ERROR_INVALID_PARAMETER`, Windows' own documented
    result for "no process with this PID currently exists", is reported
    as `False`. Every other failure — `ERROR_ACCESS_DENIED` (the process
    exists, owned by another user/session, but cannot be queried
    further), a resource/system failure, or any other unrecognized
    error — is genuinely ambiguous about the *current* state and is
    reported as `None`, never `False`. This mirrors the POSIX branch's
    own `PermissionError` handling below (an `EPERM` from `os.kill` also
    means "exists, but I can't tell more" -> treated as alive there,
    since a signal-0 `EPERM` is unambiguous proof of existence; an
    `OpenProcess` failure other than `ERROR_INVALID_PARAMETER` carries no
    such guarantee either way, hence `None` rather than a guess).
    """
    try:
        import ctypes

        if kernel32 is None:
            kernel32 = _load_kernel32()
        if get_last_error is None:
            get_last_error = ctypes.get_last_error

        handle = kernel32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            if get_last_error() == _WIN_ERROR_INVALID_PARAMETER:
                # Windows' own "no such process" result — confidently dead.
                return False
            # ERROR_ACCESS_DENIED, a resource/system failure, or any other
            # unrecognized error: genuinely indeterminate, never "dead".
            return None
        try:
            exit_code = ctypes.c_ulong()
            # `ctypes.pointer(...)` rather than `byref(...)`: functionally
            # equivalent for a real Win32 call, but also a real, readable
            # ctypes object a test double can write through (`.contents.value`)
            # — `byref(...)`'s lightweight result is only valid to pass
            # into an actual FFI call, not to introspect from Python.
            if not kernel32.GetExitCodeProcess(handle, ctypes.pointer(exit_code)):
                return None
            return exit_code.value == _WIN_STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - liveness is best-effort, never fatal
        return None


def run_with_single_retry(
    operation: Callable[[], T],
    *,
    on_retry: Callable[[TransientOperationalError, int], None] | None = None,
) -> T:
    """Execute `operation`, retrying exactly once on a transient failure.

    Only :class:`TransientOperationalError` triggers the retry
    (research.md §0.6) — any other exception propagates immediately
    without a retry attempt, and a second `TransientOperationalError`
    after the retry also propagates (the caller is then responsible for
    recording ``BLOCKED_MANUAL``). This is the single, fixed-ceiling
    retry policy shared by every actor and Git invocation — not a
    general-purpose retry/backoff system.
    """
    try:
        return operation()
    except TransientOperationalError as first_error:
        if on_retry is not None:
            on_retry(first_error, 1)
        return operation()
