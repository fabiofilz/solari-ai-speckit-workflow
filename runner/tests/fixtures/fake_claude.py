"""Fake `claude` CLI stub for tests (research.md §18, tasks.md T026).

A standalone script invoked as `sys.executable <this file>` — never through
a shell, and never a real, paid model call (research.md §18's explicit
requirement that the standard test suite never invokes a real `claude`/
`codex` CLI). `actors/claude.py` (T033, a later phase of this feature, not
implemented yet) will eventually point at this script via an
executable-path override during tests.

Deliberately controlled entirely through **environment variables**, not
argv: `actors/claude.py`'s own argv contract (flag names, ordering) is not
yet decided (it belongs to T033), so this fixture accepts and ignores
whatever argv it is called with rather than guessing that future shape —
keeping the fixture stable regardless of how T033 eventually builds its
argv. This mirrors `fake_codex.py`'s own control surface.

Environment variables:

- ``FAKE_CLAUDE_STATUS``: one of ``COMPLETE`` | ``ARCHITECTURAL_DECISION_REQUIRED``
  | ``BLOCKED`` (research.md §9's three real values), or two additional
  test-only values — ``MISSING`` (emit no trailing ``STATUS:`` marker at
  all) and ``MALFORMED`` (emit an unparseable marker line). Defaults to
  ``COMPLETE``.
- ``FAKE_CLAUDE_EXIT_CODE``: process exit code to return. Defaults to ``0``.
- ``FAKE_CLAUDE_TRANSIENT_FAILURE``: when set to ``"1"``, the script exits
  immediately with :data:`TRANSIENT_FAILURE_EXIT_CODE` and prints nothing
  but a diagnostic to stderr — no ``STATUS:`` marker, nothing on stdout —
  simulating an operational launch failure so a caller's single-retry
  policy (research.md §0.6) has something real to exercise. Takes priority
  over every other variable.
- ``FAKE_CLAUDE_TRANSCRIPT``: extra text emitted to stdout before the
  marker line, so a caller can assert transcript capture/streaming
  end-to-end. Defaults to empty (nothing extra emitted).

Exit code and ``STATUS:`` value are independent knobs on purpose: a test
can, for example, script ``STATUS: COMPLETE`` alongside a nonzero exit
code to exercise a caller's "the marker and the exit code disagree" edge
case, or a malformed/missing marker together with exit code ``0`` to
exercise the fail-closed-to-``BLOCKED`` parsing rule (research.md §9).
"""

from __future__ import annotations

import os
import sys

TRANSIENT_FAILURE_EXIT_CODE = 17


def main() -> int:
    if os.environ.get("FAKE_CLAUDE_TRANSIENT_FAILURE") == "1":
        print("fake_claude: simulated transient launch failure", file=sys.stderr)
        return TRANSIENT_FAILURE_EXIT_CODE

    transcript = os.environ.get("FAKE_CLAUDE_TRANSCRIPT", "")
    if transcript:
        print(transcript)

    status = os.environ.get("FAKE_CLAUDE_STATUS", "COMPLETE")
    if status == "MISSING":
        pass  # no trailing marker line at all
    elif status == "MALFORMED":
        print("STATUS: not-a-real-value")
    else:
        print(f"STATUS: {status}")

    try:
        return int(os.environ.get("FAKE_CLAUDE_EXIT_CODE", "0"))
    except ValueError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
