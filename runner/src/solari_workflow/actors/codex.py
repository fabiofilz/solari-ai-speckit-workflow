"""Codex CLI integration + `RESULT:`/`FINDINGS:` contract parsing
(research.md §10, `contracts/codex-gate-result-contract.md`, T035).

Always a NEW session - there is no `session_mode`/`--resume` concept for
Codex at all (FR-008, unconditionally); `run_codex_gate_session` takes no
such parameter, structurally, rather than merely documenting "always
pass NEW". Codex never decides checkpoint eligibility either - this
module only parses Codex's own structured verdict; comparing findings
against `[checkpoint].blocking_severities` stays the caller's job
(`run-codex-gate`, T039).

Flag mapping lives in one isolated function, `_build_codex_argv`, mapping
`actors/claude.py`'s own pattern. Flag names here were verified against
the installed `codex` CLI's real `codex exec --help` output at
implementation time: `exec` (non-interactive), `-s`/`--sandbox
read-only` (the tightest tool-permission profile the installed CLI
exposes - T048's SHOULD-if-available layer, since a read-only sandbox
cannot stage/commit/merge/tag regardless of Codex's own verdict), and
`-c key=value` (generic TOML-valued config overrides, used here for
`approval_policy="never"` so a non-interactive gate can never stall
waiting on a human approval prompt).

**T048/M5 - Git-prohibition enforcement, and its three DISTINCT layers**
(mirrors `actors/claude.py`'s own identical distinction - never conflate
these in code, tests, or reports):

1. **Prompt/contract prohibition** (`_GIT_PROHIBITION_TEXT`, embedded in
   the prompt text since `codex exec` has no separate system-prompt
   flag): a policy instruction, zero technical enforcement on its own.
2. **CLI permission restriction** (`--sandbox read-only`): the strongest
   sandbox mode the installed `codex` CLI exposes (`read-only,
   workspace-write, danger-full-access` are the only three - verified
   against its own `codex exec --help` output at implementation time). A
   read-only sandbox should refuse any filesystem write, which would
   include a `.git` mutation - but this module makes no claim stronger
   than "the strongest enforceable restriction available in the
   currently installed CLI, applied"; it is not a proof that every
   possible Git-invoking code path is blocked.
3. **Runner integrity backstop** (the only layer this codebase can
   actually PROVE): the before/after Candidate Tree comparison
   `run-codex-gate` (T039) performs immediately around every
   `run_codex_gate_session` call - `Codex gate modified the working
   tree` is a hard failure on ANY tree-OID drift, Git-command-caused or
   otherwise (research.md §10/§12), regardless of whether layers 1/2
   held. See also `checkpoint`'s own fingerprint recheck (T040/T043) and
   the real staging-area precondition (T045), which independently catch
   a stray Git mutation that survives past the gate.

**Prompt delivery**: unlike `run-claude`, `run-codex-gate`'s own CLI
synopsis (`contracts/cli-interface.md`) takes NO orchestrator-supplied
free-text argument at all - the review context is entirely Runner-
authored (task range/titles, a Runner-generated diff via `git/ops.py`).
`run_codex_gate_session` therefore takes a single `prompt_body` the
caller (`run-codex-gate`, T039) has already assembled from that Runner-
owned context; this module only appends the fixed Git-prohibition and
`RESULT:`/`FINDINGS:` contract instructions Codex needs to see (research.md
§0.12: "any repository context Codex needs is prepared and handed to it
by the Runner... Codex consumes this as ordinary text").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from solari_workflow.actors import protocol

__all__ = [
    "CODEX_EXECUTABLE",
    "VALID_SEVERITIES",
    "CodexFinding",
    "CodexGateResult",
    "CodexSessionOutcome",
    "CodexInvocationError",
    "parse_codex_result",
    "is_checkpoint_eligible",
    "verify_codex_capabilities",
    "run_codex_gate_session",
]

from solari_workflow.errors import UnsupportedCLICapabilityError, WorkflowError
from solari_workflow.platform import proc

CODEX_EXECUTABLE = "codex"

VALID_SEVERITIES: tuple[str, ...] = ("blocker", "major", "minor", "info")

# Verified against the installed `codex` CLI's own `codex exec --help`
# output at implementation time (research.md §10) - not assumed.
_REQUIRED_FLAGS: tuple[str, ...] = ("exec", "--sandbox", "-c")

_GIT_PROHIBITION_TEXT = (
    "You are acting as an independent, read-only reviewer under the Solari "
    "AI Spec Kit Workflow. You MUST NOT execute any Git command under any "
    "circumstances (git status, git diff, git add, git commit, git checkout, "
    "git switch, git branch, git merge, git rebase, git reset, git stash, "
    "git tag, git push, or any branch deletion), and you MUST NOT stage, "
    "commit, merge, or tag anything, regardless of your own verdict. Review "
    "only the working directory content and the context already provided to "
    "you below; do not attempt to obtain a diff or status yourself."
)

_RESULT_CONTRACT_SUFFIX = """

---
You MUST end your ENTIRE response with a plain, UNFENCED block of exactly
this shape (do NOT wrap it in ``` or ~~~ fences and do NOT quote or indent
it), as the very last thing in your response:

RESULT: PASS
FINDINGS:
- severity: blocker|major|minor|info
  summary: <one-line description>
  location: <file:line or area, optional>

The RESULT: line must be exactly PASS or FAIL (case-sensitive) - no other
value is recognized. FINDINGS: is mandatory even when there are none - write
`FINDINGS: []` in that case. Each finding MUST declare a severity from
exactly {blocker, major, minor, info} AND a summary - a finding missing
either field invalidates this entire block. Do not write anything after
this block.

The runner parses RESULT:/FINDINGS: with a strict machine protocol: both
MUST start at the very beginning of their own line (no leading spaces or
other characters), MUST NOT appear inside a fenced code block or a quoted
line, and MUST be the only such occurrence anywhere in your response - do
not show an example of this format earlier in your answer, since the
runner cannot tell an example apart from the real block and will treat
the whole response as unparseable (FAIL) if it sees more than one.
"""


class CodexInvocationError(WorkflowError):
    """A non-retryable failure preparing a `codex` invocation."""


@dataclass(frozen=True)
class CodexFinding:
    severity: str
    summary: str
    location: str | None = None


@dataclass(frozen=True)
class CodexGateResult:
    """The parsed trailing block (`contracts/codex-gate-result-contract.md`).

    `parsed` is `False` for anything missing/malformed - in which case
    `result` is always forced to `"FAIL"` and `findings` is empty,
    matching the contract's fail-closed default exactly ("the runner
    never defaults an ambiguous or missing result to PASS").
    """

    result: str
    findings: tuple[CodexFinding, ...]
    parsed: bool


_RESULT_LINE_RE = re.compile(r"^RESULT:\s?(.*)$")
_FINDINGS_HEADER_RE = re.compile(r"^FINDINGS:\s*(\[\s*\])?\s*$")
_FINDING_ITEM_RE = re.compile(r"^-\s*severity:\s*(?P<severity>\S+)\s*$")
_FINDING_FIELD_RE = re.compile(r"^(?P<key>summary|location):\s*(?P<value>.*)$")

_UNPARSEABLE = CodexGateResult(result="FAIL", findings=(), parsed=False)


def parse_codex_result(stdout: str) -> CodexGateResult:
    """Deterministic parser for the required trailing block - fail-closed
    to `FAIL` (never `PASS`) for anything missing, malformed, ambiguous,
    or case-mismatched, per `contracts/codex-gate-result-contract.md`
    (B2/item-2 remediation).

    A strict machine protocol (`actors/protocol.py`), not a prose search:
    a candidate `RESULT:` line must start at COLUMN 0, outside any fenced
    code block or blockquote line - an indented/quoted/fenced example
    (`"Example:\\n  RESULT: PASS"`) never qualifies as a candidate at
    all. "Last marker wins" is explicitly rejected: if more than one
    qualifying candidate exists anywhere, the whole output is ambiguous
    and fails closed, regardless of agreement or well-formedness; only
    exactly one is ever accepted as the authoritative trailing block, and
    only when the `FINDINGS:` header immediately following it (skipping
    blank lines) ALSO starts at column 0. Every finding must additionally
    satisfy the full formal schema - a `severity` from the fixed set AND
    a `summary` field - or the entire result is unparseable; a finding
    with only a `severity` and no `summary` is not a partial pass, it
    invalidates the whole block.
    """
    lines = stdout.splitlines()

    result_indices = protocol.find_candidate_marker_lines(lines, "RESULT:")
    if len(result_indices) != 1:
        return _UNPARSEABLE
    result_idx = result_indices[0]

    match = _RESULT_LINE_RE.match(lines[result_idx])
    value = match.group(1).strip() if match else ""
    if value not in ("PASS", "FAIL"):
        return _UNPARSEABLE

    rest = lines[result_idx + 1 :]
    idx = 0
    while idx < len(rest) and not rest[idx].strip():
        idx += 1
    if idx >= len(rest) or not rest[idx].startswith("FINDINGS:") or not _FINDINGS_HEADER_RE.match(rest[idx]):
        return _UNPARSEABLE

    findings: list[CodexFinding] = []
    current: dict[str, str] | None = None
    for raw_line in rest[idx + 1 :]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        item_match = _FINDING_ITEM_RE.match(stripped)
        if item_match:
            if current is not None:
                finalized = _finalize_finding(current)
                if finalized is None:
                    return _UNPARSEABLE
                findings.append(finalized)
            current = {"severity": item_match.group("severity")}
            continue
        field_match = _FINDING_FIELD_RE.match(stripped)
        if field_match and current is not None:
            current[field_match.group("key")] = field_match.group("value").strip()
            continue
        return _UNPARSEABLE  # a line inside the block that fits no recognized shape
    if current is not None:
        finalized = _finalize_finding(current)
        if finalized is None:
            return _UNPARSEABLE
        findings.append(finalized)

    for finding in findings:
        if finding.severity not in VALID_SEVERITIES:
            return _UNPARSEABLE

    return CodexGateResult(result=value, findings=tuple(findings), parsed=True)


def _finalize_finding(data: dict[str, str]) -> CodexFinding | None:
    """`None` means the finding does not satisfy the formal schema (item
    2: "every finding must satisfy the formal schema, including required
    summary fields") - the caller treats this as making the ENTIRE result
    unparseable, never a silently-dropped or partially-accepted finding.
    """
    if "summary" not in data or not data["summary"]:
        return None
    return CodexFinding(severity=data.get("severity", ""), summary=data["summary"], location=data.get("location"))


def is_checkpoint_eligible(result: CodexGateResult, blocking_severities: list[str]) -> bool:
    """`RESULT: PASS` alone is never sufficient - eligibility is decided
    by the Runner's own `[checkpoint].blocking_severities` policy against
    the parsed findings list, never by Codex's own top-line verdict
    (`contracts/codex-gate-result-contract.md`: "Codex must not waive its
    own failures"). An unparsed/`FAIL` result is never eligible."""
    if not result.parsed or result.result != "PASS":
        return False
    return not any(finding.severity in blocking_severities for finding in result.findings)


def verify_codex_capabilities(help_text: str) -> None:
    missing = [flag for flag in _REQUIRED_FLAGS if flag not in help_text]
    if missing:
        raise UnsupportedCLICapabilityError(
            "installed 'codex' CLI does not appear to support required flag(s)/subcommand(s): "
            + ", ".join(missing)
            + " (checked 'codex exec --help' output; verify the installed version)"
        )


def _build_codex_argv() -> list[str]:
    """The ONE place `codex`'s argv shape is decided (mirrors T033's
    `_build_claude_argv`). `--sandbox read-only` is the technical,
    SHOULD-if-available backstop against Codex mutating anything
    (T048) - on top of, never instead of, the Candidate Tree before/
    after comparison (research.md §10/§12), which is unconditional."""
    return ["exec", "--sandbox", "read-only", "-c", 'approval_policy="never"']


def _looks_like_operational_launch_failure(result: proc.ProcessResult) -> bool:
    """Mirrors `actors/claude.py`'s own heuristic exactly - see its
    docstring for the rationale."""
    return result.returncode != 0 and not result.stdout.strip()


def run_codex_gate_session(
    *,
    prompt_body: str,
    cwd: str,
    argv_prefix: list[str] | None = None,
    env: dict[str, str] | None = None,
    verify_capabilities: bool = True,
) -> CodexSessionOutcome:
    """Invoke `codex` for one independent, read-only gate.

    `argv_prefix` overrides the resolved `codex` executable, exactly like
    `run_claude_session`'s own parameter - tests pass
    `[sys.executable, str(FAKE_CODEX)]`.
    """
    prefix = list(argv_prefix) if argv_prefix is not None else [proc.resolve_executable(CODEX_EXECUTABLE)]

    if verify_capabilities:
        help_result = proc.run([*prefix, "exec", "--help"], cwd=cwd, env=env, check=False)
        verify_codex_capabilities(help_result.stdout)

    argv = _build_codex_argv()
    full_argv = [*prefix, *argv]
    full_prompt = _GIT_PROHIBITION_TEXT + "\n\n" + prompt_body + _RESULT_CONTRACT_SUFFIX

    def _invoke() -> proc.ProcessResult:
        try:
            result = proc.run_streaming(full_argv, cwd=cwd, env=env, input_text=full_prompt)
        except OSError as exc:
            raise proc.TransientOperationalError(f"failed to launch 'codex': {exc}") from exc
        if _looks_like_operational_launch_failure(result):
            raise proc.TransientOperationalError(
                f"'codex' exited {result.returncode} with no output at all "
                f"(stderr: {result.stderr.strip()!r}); treating as an operational launch failure"
            )
        return result

    result = proc.run_with_single_retry(_invoke)

    # B2: process-level success is a PREREQUISITE to semantic parsing. A
    # nonzero exit is never overridden by a `RESULT: PASS` in stdout -
    # mirrors `actors/claude.py`'s own identical fix; see its docstring.
    if result.returncode != 0:
        parsed = _UNPARSEABLE
    else:
        parsed = parse_codex_result(result.stdout)
    return CodexSessionOutcome(parsed=parsed, transcript=result.stdout, argv=tuple(full_argv), raw_result=result)


@dataclass(frozen=True)
class CodexSessionOutcome:
    parsed: CodexGateResult
    transcript: str
    argv: tuple[str, ...]
    raw_result: proc.ProcessResult
