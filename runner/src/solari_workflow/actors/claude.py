"""Claude Code CLI integration + `STATUS:` contract parsing (research.md §9, T033).

`run_claude_session` is the single entry point `run-claude` (T038) uses.
It never infers model/effort/session-mode — all three are required
keyword arguments with no defaults, so a caller that omits one gets a
`TypeError` from Python itself, not a silently-assumed value (research.md
§9: "the runner itself never defaults to or infers CONTINUE").

Flag mapping lives in exactly one isolated function, `_build_claude_argv`,
per T033's own explicit requirement — every flag name here was verified
against the installed `claude` CLI's real `--help` output at
implementation time (`--print`, `--model`, `--effort`, `--resume`,
`--session-id`, `--disallowedTools`, `--append-system-prompt` all exist in
the CLI this was implemented against); a future CLI that drops one of
these is caught by :func:`verify_claude_capabilities` before any session
is spent, per the "fail closed rather than assume" posture research.md §9
requires.

**T048/M5 - Git-prohibition enforcement, and its three DISTINCT layers**
(never to be conflated with one another, in code, tests, or reports):

1. **Prompt/contract prohibition** (`_GIT_PROHIBITION_SYSTEM_PROMPT`,
   `--append-system-prompt`): a policy instruction Claude is asked to
   follow. Zero technical enforcement on its own - a model can, in
   principle, ignore a system prompt.
2. **CLI permission restriction** (`--disallowedTools "Bash(git *)"
   "Bash(*git*)"`, applied unconditionally by `_build_claude_argv`
   below): a best-effort, CLI-level layer that the installed `claude`
   CLI does support (verified against its own `--help` output at
   implementation time) and does deny a literal or wrapped `git`
   invocation through the Bash tool specifically. This is NOT a proven
   technical guarantee that "no Git command whatsoever" can ever occur -
   it says nothing about a hypothetical future tool surface, and this
   module makes no claim stronger than "the strongest enforceable
   restriction available in the currently installed CLI, applied."
3. **Runner integrity backstop** (the only layer this codebase can
   actually PROVE): `checkpoint`'s own fingerprint recheck (T040,
   `git/checkpoint.py::preflight` / the exact `CHECKPOINT BLOCKED —
   WORKING TREE CHANGED AFTER GATE` message, T043's own integration
   test) and the real staging-area precondition
   (`git/ops.py::GitRepo.check_staging_precondition`, T045's own
   integration test) together detect - unconditionally, regardless of
   whether layers 1/2 held - a stray `git add`/`git commit` made between
   a passing gate and checkpoint, since either changes `head_oid` or the
   real index's relationship to `HEAD`. This is the actual, verifiable
   guarantee: not "Claude cannot run Git," but "an unauthorized Git
   mutation, however it happened, is caught before it can be
   checkpointed."

**Prompt delivery — an interpretive choice, documented here rather than
silently assumed**: `contracts/cli-interface.md`'s `run-claude` synopsis
declares exactly `--model`/`--effort`/`--session-mode`/`--session-id`/
`--purpose` and no separate "instructions" flag. Rather than inventing an
undocumented channel (e.g. a new `--prompt-file` flag, or reading
arbitrary stdin content the contract never mentions), this module treats
`--purpose`'s own "free text" as both the audit-trail label AND the
literal substance of Claude's task instructions — the only content
channel the contract actually defines. The Runner appends its own fixed
Git-prohibition instruction (`--append-system-prompt`) and `STATUS:`
contract suffix on top of that text; the Orchestrator is responsible for
composing `--purpose` with whatever task/remediation detail a block
needs. This does mean a very long `--purpose` value reaches `claude` as
one argv token by way of one command-line argument to `solari-workflow`
itself — acceptable for v1 (Windows' ~32K-character command-line ceiling
is far beyond any task description exercised by this workflow's own
tests), but worth flagging honestly rather than pretending an unbounded
channel exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from solari_workflow.actors import protocol
from solari_workflow.errors import UnsupportedCLICapabilityError, WorkflowError
from solari_workflow.platform import proc

CLAUDE_EXECUTABLE = "claude"

STATUS_VALUES: tuple[str, ...] = ("COMPLETE", "ARCHITECTURAL_DECISION_REQUIRED", "BLOCKED")
SESSION_MODES: tuple[str, ...] = ("NEW", "CONTINUE")

# Verified against the installed `claude` CLI's own `--help` output at
# implementation time (research.md §9) - not assumed. `--disallowedTools`
# and `--append-system-prompt` are also production-relied-upon flags
# (m1 remediation: capability verification must cover every flag actually
# used, not merely the model/effort/session-mode surface).
_REQUIRED_FLAGS: tuple[str, ...] = (
    "--print",
    "--model",
    "--effort",
    "--disallowedTools",
    "--append-system-prompt",
)
_CONTINUE_FLAG = "--resume"
_STATUS_LINE_RE = re.compile(r"^STATUS:\s?(.*)$")

_GIT_PROHIBITION_SYSTEM_PROMPT = (
    "You are operating under the Solari AI Spec Kit Workflow. You MUST NOT "
    "execute any Git command under any circumstances, including but not "
    "limited to: git status, git diff, git add, git commit, git checkout, "
    "git switch, git branch, git merge, git rebase, git reset, git stash, "
    "git tag, git push, or any branch deletion. The branch you are working "
    "on already exists and is already checked out by the Runner before this "
    "session began. Your responsibility begins and ends at the content of "
    "the working tree's files - read, create, and modify project files, and "
    "run non-Git project commands (test/lint/build) as needed, but never "
    "stage, commit, or otherwise finalize anything via Git."
)

_STATUS_CONTRACT_SUFFIX = """

---
Before you finish, you MUST end your ENTIRE response with exactly one line
of the following form, and nothing after it:

STATUS: COMPLETE
STATUS: ARCHITECTURAL_DECISION_REQUIRED
STATUS: BLOCKED

Use COMPLETE only once implementation and self-validation are finished and
safe to hand off for independent review. Use ARCHITECTURAL_DECISION_REQUIRED
when completing this task would require an out-of-scope architectural
decision rather than one you are authorized to make yourself - stop and
describe the decision needed instead of deciding it. Use BLOCKED when you
cannot complete the task for a describable reason that is not an
architectural decision. Emit exactly one such line, and make it the very
last line of your response.

The runner parses this line with a strict machine protocol: it MUST start
at the very beginning of its own line (no leading spaces or other
characters), it MUST NOT appear inside a fenced code block or a quoted
line, and it MUST be the only such line anywhere in your response - do
not show an example of this format earlier in your answer, since the
runner cannot tell an example apart from the real marker and will treat
the whole response as unparseable if it sees more than one.
"""


class ClaudeInvocationError(WorkflowError):
    """A non-retryable failure preparing or interpreting a `claude` invocation
    (an invalid session-mode/session-id combination, or a missing required
    argument) - never raised for a real session's own reported outcome,
    which always fails closed to `BLOCKED` instead (see `_parse_status`)."""


@dataclass(frozen=True)
class ClaudeSessionOutcome:
    """The result of one `run_claude_session` call - always has a
    `status` in :data:`STATUS_VALUES`, fail-closed to `"BLOCKED"` for a
    missing/unparseable trailing marker (research.md §9)."""

    status: str
    transcript: str
    argv: tuple[str, ...]
    raw_result: proc.ProcessResult


def verify_claude_capabilities(help_text: str, *, session_mode: str) -> None:
    """Fail closed if the installed `claude` CLI's own `--help` output
    does not advertise a flag this invocation needs (research.md §9's
    "fails closed... if the installed CLI does not support a requested
    capability" - never silently dropping the request or falling back to
    an unrequested mode)."""
    required = list(_REQUIRED_FLAGS)
    if session_mode == "CONTINUE":
        required.append(_CONTINUE_FLAG)
    missing = [flag for flag in required if flag not in help_text]
    if missing:
        raise UnsupportedCLICapabilityError(
            "installed 'claude' CLI does not appear to support required flag(s): "
            + ", ".join(missing)
            + " (checked its own --help output; verify the installed version)"
        )


def _build_claude_argv(
    *,
    model: str,
    effort: str,
    session_mode: str,
    session_id: str | None,
) -> list[str]:
    """The ONE place `claude`'s argv shape is decided (T033's own explicit
    requirement for an isolated flag-mapping function)."""
    if not model:
        raise ClaudeInvocationError("model is required and must be non-empty")
    if not effort:
        raise ClaudeInvocationError("effort is required and must be non-empty")
    if session_mode not in SESSION_MODES:
        raise ClaudeInvocationError(
            f"invalid session_mode {session_mode!r}; must be one of {SESSION_MODES}"
        )
    if session_mode == "CONTINUE" and not session_id:
        raise ClaudeInvocationError(
            "session_mode=CONTINUE requires an explicit --session-id (FR-009); "
            "the runner never defaults or infers a session to resume"
        )

    argv = ["--print", "--model", model, "--effort", effort]
    if session_mode == "CONTINUE":
        argv += [_CONTINUE_FLAG, str(session_id)]
    # M5: two overlapping patterns, not one - "Bash(git *)" denies a
    # direct `git ...` invocation; "Bash(*git*)" additionally denies a
    # wrapped/prefixed invocation (e.g. `cd x && git ...`, an absolute
    # `/usr/bin/git ...`, or `command git ...`). This is still a
    # best-effort CLI-level permission restriction, not a proven
    # sandbox - see the module docstring's "T048 - Git-prohibition
    # enforcement" section for the actual unconditional guarantee
    # (the Runner-side Candidate Tree/fingerprint/staging-precondition
    # backstops).
    argv += ["--disallowedTools", "Bash(git *)", "Bash(*git*)"]
    argv += ["--append-system-prompt", _GIT_PROHIBITION_SYSTEM_PROMPT]
    return argv


def _looks_like_operational_launch_failure(result: proc.ProcessResult) -> bool:
    """Heuristic distinguishing a transient/operational launch failure from
    an ordinary (even if unparseable) session outcome (research.md §0.6):
    a nonzero exit with completely empty stdout - no transcript, no
    marker, nothing - is what a process that never got the chance to run
    a real session looks like. A session that ran far enough to emit
    *any* stdout (a partial transcript, a malformed marker, a `BLOCKED`
    line) is deliberately NOT reclassified as transient here - that is
    the fail-closed-to-`BLOCKED` path (`_parse_status`), never retried
    (research.md §9's "missing or unparseable marker -> BLOCKED" is a
    content-interpretation rule, not an operational-failure signal)."""
    return result.returncode != 0 and not result.stdout.strip()


def _parse_status(stdout: str) -> str:
    """The trailing `STATUS:` marker, fail-closed to `"BLOCKED"` for
    anything missing, unparseable, or AMBIGUOUS (research.md §9; B2/
    item-2 remediation).

    A strict machine protocol, not a prose search (`actors/protocol.py`):
    a candidate marker line must start with `STATUS:` at COLUMN 0 (never
    merely after `.strip()`, which would authorize an indented/quoted
    example), and must not fall inside a fenced code block or a
    blockquote line. "Last marker wins" is explicitly rejected: if more
    than one qualifying candidate exists anywhere (a duplicate, a
    conflicting value, or one that happens to also start at column 0
    elsewhere), the whole output is ambiguous and fails closed,
    regardless of agreement or well-formedness. Only when EXACTLY ONE
    candidate exists, AND it is also the true trailing content (nothing
    but blank lines follow it), AND its value is one of
    :data:`STATUS_VALUES`, is it accepted.
    """
    lines = stdout.splitlines()
    marker_indices = protocol.find_candidate_marker_lines(lines, "STATUS:")
    if len(marker_indices) != 1:
        return "BLOCKED"

    marker_index = marker_indices[0]
    match = _STATUS_LINE_RE.match(lines[marker_index])
    value = match.group(1).strip() if match else ""
    if value not in STATUS_VALUES:
        return "BLOCKED"

    if not protocol.is_genuinely_trailing(lines, marker_index):
        return "BLOCKED"

    return value


def run_claude_session(
    *,
    model: str,
    effort: str,
    session_mode: str,
    session_id: str | None,
    prompt_body: str,
    cwd: str,
    argv_prefix: list[str] | None = None,
    env: dict[str, str] | None = None,
    verify_capabilities: bool = True,
) -> ClaudeSessionOutcome:
    """Invoke `claude` for one implementation/remediation execution.

    `argv_prefix` overrides the resolved `claude` executable with an
    arbitrary argv prefix (research.md §18: "Tests point the runner at
    these via an executable-path override") - tests pass
    `[sys.executable, str(FAKE_CLAUDE)]` here. Defaults to the real,
    `PATH`-resolved `claude` executable.

    Applies the single-retry-then-give-up policy (research.md §0.6) for a
    genuinely operational launch failure via `proc.run_with_single_retry`;
    every other outcome (including a parsed `BLOCKED`, or a missing/
    unparseable marker that fails closed to `BLOCKED`) is returned
    directly, never retried, matching the retry table's own restriction
    to transient/operational failures only.
    """
    prefix = list(argv_prefix) if argv_prefix is not None else [proc.resolve_executable(CLAUDE_EXECUTABLE)]

    if verify_capabilities:
        help_result = proc.run([*prefix, "--help"], cwd=cwd, env=env, check=False)
        verify_claude_capabilities(help_result.stdout, session_mode=session_mode)

    argv = _build_claude_argv(model=model, effort=effort, session_mode=session_mode, session_id=session_id)
    full_argv = [*prefix, *argv]
    full_prompt = prompt_body + _STATUS_CONTRACT_SUFFIX

    def _invoke() -> proc.ProcessResult:
        try:
            result = proc.run_streaming(full_argv, cwd=cwd, env=env, input_text=full_prompt)
        except OSError as exc:
            raise proc.TransientOperationalError(f"failed to launch 'claude': {exc}") from exc
        if _looks_like_operational_launch_failure(result):
            raise proc.TransientOperationalError(
                f"'claude' exited {result.returncode} with no output at all "
                f"(stderr: {result.stderr.strip()!r}); treating as an operational launch failure"
            )
        return result

    result = proc.run_with_single_retry(_invoke)

    # B2: process-level success is a PREREQUISITE to semantic marker
    # parsing. A nonzero exit is never overridden by a `STATUS: COMPLETE`
    # (or any other) marker in stdout - `_looks_like_operational_launch_
    # failure` above already retried the "launched but produced nothing"
    # case; reaching here with a nonzero exit means the process ran far
    # enough to print *something*, but still did not report a genuine
    # success, and that "something" (even a well-formed marker) is not
    # trustworthy evidence of a completed session.
    if result.returncode != 0:
        status = "BLOCKED"
    else:
        status = _parse_status(result.stdout)
    return ClaudeSessionOutcome(status=status, transcript=result.stdout, argv=tuple(full_argv), raw_result=result)
