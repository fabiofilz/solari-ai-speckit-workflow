"""Strict actor-protocol marker detection, shared by `actors/claude.py`
(`STATUS:`) and `actors/codex.py` (`RESULT:`) — B2/item-2 remediation,
tilde fences (third remediation, M2), structural fence closers (fourth
remediation, B1).

Parsing is a strict machine protocol, never a prose search for a
substring. A line is a *candidate* authoritative marker only when:

- it starts with the marker prefix at **column 0** — checked against the
  RAW line, never `line.strip()`; any leading whitespace or a blockquote
  `>` disqualifies it;
- it is **not inside a fenced code block** of either Markdown fence
  family — backtick (```` ``` ````) or tilde (`~~~`).

The authoritative marker block is therefore always UNFENCED; the actor
contracts (`contracts/codex-gate-result-contract.md`, the Claude/Codex
prompt suffixes) instruct exactly that.

Fence grammar (CommonMark-shaped, deliberately fail-closed):

- A line's **container** is its blockquote depth: repeatedly consume
  `0-3 spaces`, `>`, and one optional space. Whatever remains is the
  line's content at that depth.
- An **opener** is content with `0-3` leading spaces followed by a run of
  three or more backticks or three or more tildes. A backtick opener's
  info string must not contain a backtick (otherwise the line is inline
  code, not a fence). Four or more spaces (or a tab) of indentation is
  never an opener.
- A **closer** for an open fence must be at EXACTLY the opener's quote
  depth, have `0-3` leading spaces, a run of the SAME fence character at
  least as long as the opener's run, and nothing after it but spaces or
  tabs. So a blockquoted apparent closer never closes an unquoted fence,
  a four-space-indented apparent closer never closes anything, and the
  two fence families never close each other.
- Leaving a blockquote does NOT implicitly close a fence opened inside it
  (a deliberate fail-closed deviation from CommonMark: it can only hide
  text, never expose it).
- An **unclosed fence stays open through EOF**: every later line is
  fenced and can never be authoritative.

Exactly one candidate is required for a marker to be authoritative at
all — enforced by the caller; zero or more-than-one both fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_QUOTE_MARKER_RE = re.compile(r"^ {0,3}> ?")
_FENCE_OPENER_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CLOSER_TRAILING_RE = re.compile(r"^[ \t]*$")


@dataclass(frozen=True)
class _OpenFence:
    quote_depth: int
    char: str
    length: int


def _split_quote_prefix(line: str) -> tuple[int, str]:
    """`(blockquote depth, remaining content)` for `line`."""
    depth = 0
    rest = line
    while True:
        match = _QUOTE_MARKER_RE.match(rest)
        if match is None:
            return depth, rest
        depth += 1
        rest = rest[match.end() :]


def _fence_opener(line: str) -> _OpenFence | None:
    depth, content = _split_quote_prefix(line)
    match = _FENCE_OPENER_RE.match(content)
    if match is None:
        return None
    run, info = match.group(1), match.group(2)
    if run[0] == "`" and "`" in info:
        return None
    return _OpenFence(quote_depth=depth, char=run[0], length=len(run))


def _closes_fence(line: str, fence: _OpenFence) -> bool:
    depth, content = _split_quote_prefix(line)
    if depth != fence.quote_depth:
        return False
    match = _FENCE_OPENER_RE.match(content)
    if match is None:
        return False
    run, trailing = match.group(1), match.group(2)
    return run[0] == fence.char and len(run) >= fence.length and _CLOSER_TRAILING_RE.match(trailing) is not None


def find_candidate_marker_lines(lines: list[str], prefix: str) -> list[int]:
    """Indices of lines that qualify as a possible authoritative marker:
    starting with `prefix` at column 0 and outside any backtick or tilde
    fenced block (closed or unclosed).
    """
    indices: list[int] = []
    open_fence: _OpenFence | None = None
    for i, line in enumerate(lines):
        if open_fence is not None:
            if _closes_fence(line, open_fence):
                open_fence = None
            continue
        opener = _fence_opener(line)
        if opener is not None:
            open_fence = opener
            continue
        if line.startswith(prefix):
            indices.append(i)
    return indices


def is_genuinely_trailing(lines: list[str], marker_index: int) -> bool:
    """Whether nothing but blank lines follows `lines[marker_index]` —
    the authoritative marker must be the true end of the output, not
    merely well-formed and uniquely present somewhere in the middle."""
    return not any(line.strip() for line in lines[marker_index + 1 :])
