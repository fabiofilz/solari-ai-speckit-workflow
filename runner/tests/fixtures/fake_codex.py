"""Fake `codex` CLI stub for tests (research.md §18, tasks.md T026).

Mirrors `fake_claude.py`'s design exactly — a standalone script invoked as
`sys.executable <this file>`, controlled entirely through environment
variables (never argv, since `actors/codex.py`'s own argv contract belongs
to T033/T035, a later phase not implemented yet), and never a real, paid
model call.

Environment variables:

- ``FAKE_CODEX_RESULT``: one of ``PASS`` | ``FAIL`` (the only two values
  `contracts/codex-gate-result-contract.md` recognizes), or two additional
  test-only values — ``MISSING`` (emit no trailing block at all) and
  ``MALFORMED`` (emit a `RESULT:` line with a value the contract does not
  recognize, e.g. case-mismatched `"Pass"`). Defaults to ``PASS``.
- ``FAKE_CODEX_FINDINGS``: a ``;``-separated list of ``severity:summary``
  pairs (e.g. ``"blocker:missing null check;minor:typo in comment"``)
  rendered as the trailing block's `FINDINGS:` entries. Defaults to empty,
  which renders `FINDINGS: []` — the contract's documented "no findings"
  shape.
- ``FAKE_CODEX_EXIT_CODE``: process exit code to return. Defaults to ``0``.
- ``FAKE_CODEX_TRANSIENT_FAILURE``: when set to ``"1"``, the script exits
  immediately with :data:`TRANSIENT_FAILURE_EXIT_CODE` and prints nothing
  but a diagnostic to stderr — no trailing block at all — simulating an
  operational launch failure for the single-retry path (research.md
  §0.6). Takes priority over every other variable.

Exit code and `RESULT:` value are independent knobs, for the same reason
as `fake_claude.py`: a test can script a `RESULT: FAIL` alongside exit
code `0`, or `RESULT: PASS` alongside a nonzero exit code, to exercise a
caller's handling of each combination independently.
"""

from __future__ import annotations

import os
import sys

TRANSIENT_FAILURE_EXIT_CODE = 17

# A canned `--help` response naming every flag/subcommand
# `actors/codex.py`'s `_build_codex_argv`/`verify_codex_capabilities`
# (T035) either uses or checks for - `--help` always takes priority over
# every other knob (including a simulated transient failure), matching
# `fake_claude.py`'s own rationale exactly.
_HELP_TEXT = """Usage: codex exec [OPTIONS] [PROMPT]
  exec              Run Codex non-interactively
  -s, --sandbox <SANDBOX_MODE>   [read-only, workspace-write, danger-full-access]
  -c, --config <key=value>       Override a configuration value
  -m, --model <MODEL>            Model the agent should use
"""


def _render_findings(raw: str) -> str:
    if not raw:
        return "FINDINGS: []"
    lines = ["FINDINGS:"]
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        severity, _, summary = entry.partition(":")
        lines.append(f"- severity: {severity.strip()}")
        lines.append(f"  summary: {summary.strip()}")
    return "\n".join(lines)


def main() -> int:
    if "--help" in sys.argv[1:]:
        print(_HELP_TEXT)
        return 0

    if os.environ.get("FAKE_CODEX_TRANSIENT_FAILURE") == "1":
        print("fake_codex: simulated transient launch failure", file=sys.stderr)
        return TRANSIENT_FAILURE_EXIT_CODE

    result = os.environ.get("FAKE_CODEX_RESULT", "PASS")
    if result == "MISSING":
        pass  # no trailing block at all
    else:
        if result == "MALFORMED":
            print("RESULT: Pass")  # case-mismatched — unparseable per the contract
        else:
            print(f"RESULT: {result}")
        print(_render_findings(os.environ.get("FAKE_CODEX_FINDINGS", "")))

    try:
        return int(os.environ.get("FAKE_CODEX_EXIT_CODE", "0"))
    except ValueError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
