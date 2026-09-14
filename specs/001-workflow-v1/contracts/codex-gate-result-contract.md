# Contract: Codex Independent Gate Result

This is the structured contract the runner's Codex prompt requires, and
what `actors/codex.py`'s parser deterministically consumes (research.md
§10). It exists so a PASS/FAIL decision never depends on interpreting
Codex's free-form prose.

**Codex never executes a Git command** (research.md §0.12) — it reviews
whatever the Runner exposes on disk (which, by construction, already holds
the Candidate Tree's exact content) and any diff/context text the Runner
chooses to prepare for it; it never runs `git diff`/`git status` itself,
and it never stages, commits, merges, or tags regardless of its verdict.

## Required trailing block

Codex's session output **MUST** end with a plain, **unfenced** block of
exactly this shape (shown fenced here only for display; the actor MUST NOT
wrap it in a ```` ``` ````/`~~~` fence, quote it, or indent it):

```
RESULT: PASS|FAIL
FINDINGS:
- severity: blocker|major|minor|info
  summary: <one-line description>
  location: <file:line or area, optional>
- severity: ...
  ...
```

- `RESULT:` line is mandatory and must be exactly `PASS` or `FAIL`
  (case-sensitive, no other values).
- `FINDINGS:` is mandatory even when empty (`FINDINGS: []` or a `FINDINGS:`
  header followed by no list items is acceptable and means "no findings").
- Each finding **MUST** declare a `severity` from the fixed set `blocker |
  major | minor | info`. Any other value, or a finding missing `severity`,
  makes the entire result **unparseable**.

## Parser behavior (fail-closed)

- `RESULT:` and `FINDINGS:` are recognized only at column 0, outside any
  blockquote and outside any fenced code block (backtick or tilde; a fence
  closes only on a structurally valid closer at the same blockquote depth
  with 0-3 spaces of indentation, the same fence character, and a run at
  least as long as the opener; an unclosed fence runs to EOF). A block
  placed inside a fence is therefore never authoritative and yields
  **`FAIL`** — there is no prose or fenced fallback.
- If the trailing block is **missing entirely**, malformed (doesn't match
  the shape above), or `RESULT:` is anything other than exactly `PASS` or
  `FAIL` → the gate outcome is **`FAIL`**. The runner never defaults an
  ambiguous or missing result to `PASS`.
- If `RESULT: PASS` but at least one finding's `severity` is in
  `[checkpoint].blocking_severities` (default `["blocker", "major"]`,
  data-model.md) → the gate is recorded as **not checkpoint-eligible**,
  even though Codex itself reported `PASS`. This is the concrete mechanism
  behind "Codex must not waive its own failures": the *runner's* severity
  policy decides eligibility, not Codex's own top-line verdict.
- If `RESULT: FAIL` → the gate is `FAIL` regardless of findings content;
  Codex reporting `FAIL` is always honored (there's no scenario where the
  runner overrides a self-reported failure into a pass).

## Independent technical backstop: Candidate Tree comparison

Beyond parsing this block, the runner builds the Candidate Tree
(research.md §12 — an isolated temporary Git index, `.gitignore`-aware,
seeded from `HEAD` and populated from the current working directory;
never the real index) immediately before invoking Codex, and rebuilds it
again immediately after Codex exits. If the resulting `candidate_tree_oid`
differs, the gate is a hard failure — `Codex gate modified the working
tree` — **regardless of what the structured result block says.** A
`RESULT: PASS` with a changed Candidate Tree is still a failed gate. Note
that Codex is reviewing **uncommitted** work by design (research.md §0.9):
the Candidate Tree is exactly what's on disk relative to `HEAD`, not an
already-committed state — this backstop only fires on a Codex-caused
*change* to that content, not on the mere presence of uncommitted work.

## What gets recorded

The full parsed result (`RESULT`, the findings list, and whether it was
checkpoint-eligible under the active `blocking_severities` policy) is
written into the gate's `.ai-runs/*-result.md`, alongside Fingerprint X —
`{head_oid, candidate_tree_oid, branch_name, first_task, last_task,
computed_at}` (data-model.md) — computed at gate time. This is the single
authoritative record the `checkpoint` command re-verifies against
(`contracts/cli-interface.md`, research.md §13's invariant chain).

## Non-goals for v1

- The runner does not attempt to semantically validate finding *content*
  (e.g. whether a claimed file:line actually exists) — only the structural
  shape above.
- There is no partial-credit or fuzzy matching of `RESULT:` values (e.g.
  `"Pass"`, `"PASSED"` are both unparseable, and therefore `FAIL`) — this
  strictness is deliberate, to keep the parser's behavior simple and its
  fail-closed guarantee unconditional.
