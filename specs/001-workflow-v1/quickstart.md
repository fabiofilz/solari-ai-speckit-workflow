# Quickstart: Validating the v1 Runner (once implemented)

This is a validation guide for proving the runner behaves per this plan —
not an implementation guide. It assumes the tasks generated from this plan
(`/speckit.tasks`, then implementation) have already produced a working
`runner/` per `contracts/cli-interface.md`. **Nothing in this file has been
run yet** — v1 planning does not implement the runner (see plan.md
Summary).

## Prerequisites

- `uv` installed (research.md §17).
- `git` on `PATH`.
- For scenarios involving `claude`/`codex`: the fake stub CLIs from
  `runner/tests/fixtures/fake_claude.py` / `fake_codex.py` on `PATH` (no
  real, paid model calls needed to validate the runner itself — research.md
  §18).

## Scenario 1 — Bootstrap a throwaway project (User Story 1)

```bash
mkdir /tmp/throwaway-go-project && cd /tmp/throwaway-go-project
git init
uv tool run --from git+https://github.com/fabiofilz/solari-ai-speckit-workflow@<tag> \
  solari-workflow init --project-name throwaway-go-project --stack go
```

**Expected**: `.ai-workflow.toml` is created with `environment.stack =
"go"` and `[git.push] mode = "manual"`, with no PDF-Converter-specific
values anywhere (SC-001). `.ai-runs/` is present in `.gitignore`.

## Scenario 2 — Readiness Gate skips inapplicable sections (User Story 4)

Against the project from Scenario 1 (no Docker, no local model
configured):

```bash
solari-workflow readiness-check
```

**Expected**: exit code `0` (assuming Go manifest/lockfile are otherwise
present and tracked); output lists Docker and local-model checks as
`SKIPPED`, never `FAIL` (SC-006). Remove `go.sum` from Git tracking (leave
it on disk) and rerun — expect exit code `1` naming exactly that missing/
untracked-lockfile failure (User Story 4, Acceptance Scenario 2).

## Scenario 3 — One block end-to-end, runner-owned branch and checkpoint (User Story 2)

Using the fake `claude`/`codex` stubs configured to return a scripted
`STATUS: COMPLETE` / `RESULT: PASS` with no blocking findings:

```bash
solari-workflow start-block --first-task T001 --last-task T003 --block-name "Sample Block"
# → creates and checks out branch T001-SampleBlock (research.md §16)

solari-workflow run-claude --model fake --effort fake --session-mode NEW \
  --purpose "quickstart demo"
# → the fake claude stub edits a file on disk and leaves it UNCOMMITTED —
#   this uncommitted content is exactly what the next step reviews

solari-workflow run-codex-gate
solari-workflow checkpoint
```

**Expected**:
- `start-block` creates branch `T001-SampleBlock` — Claude never creates
  it itself (research.md §0.1/§0.10).
- `run-codex-gate` builds the Candidate Tree from the uncommitted edit
  (research.md §12), produces a `.ai-runs/*-result.md` recording `RESULT:
  PASS`, zero blocking findings, and Fingerprint X
  (`{head_oid, candidate_tree_oid, ...}`).
- `checkpoint` stages Fingerprint X's `candidate_tree_oid` into the real
  index, creates the checkpoint commit via `commit-tree`/`update-ref`,
  verifies `commit^{tree}` equals `candidate_tree_oid`, merges with
  `git merge --no-ff` into `main`, and creates annotated tag
  `checkpoint-T001-T003` containing block name, branch name, task range
  with titles, `Gate: PASS`, `Checkpoint: VERIFIED` (SC-003).
- After checkpoint, no next block starts automatically (Acceptance
  Scenario 5) — the shell prompt simply returns; `.ai-runs/.block-state.json`
  shows `state: COMPLETED`.

## Scenario 4 — Fingerprint blocks a stale checkpoint

Repeat Scenario 3 up through `run-codex-gate`, then, **before** running
`checkpoint`, make an additional edit that Codex never saw:

```bash
echo "stray change" >> README.md   # or a new untracked file
solari-workflow checkpoint
```

**Expected**: exit code `1`, stderr exactly contains
`CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE` (SC-004) — the
recomputed `candidate_tree_oid` no longer matches Fingerprint X's. Revert
the stray change, re-run `run-codex-gate` (a fresh gate, new Fingerprint
X), then `checkpoint` again — expect success.

## Scenario 5 — Concurrent execution is refused (User Story 3)

```bash
solari-workflow start-block --first-task T004 --last-task T005 --block-name "Second Block"
solari-workflow run-claude --model fake --effort fake --session-mode NEW \
  --purpose "hold the lock" &
sleep 1
solari-workflow run-codex-gate
```

**Expected**: the second invocation exits non-zero immediately, with a
message naming the first invocation's tool/pid/hostname/purpose
(research.md §7) — never runs concurrently (SC-002).

## Scenario 6 — Branch retention (User Story 5)

After checkpointing three or more blocks in sequence per Scenario 3:

```bash
solari-workflow retention --dry-run
solari-workflow retention
git branch
git tag
```

**Expected**: only the two most recently merged development branches plus
the currently active branch remain among development branches; every
checkpoint tag from all blocks is still present (SC-005).

## Scenario 7 — Operational failure, single retry, manual block, resume

Configure the fake `codex` stub to simulate a transient launch failure on
its first invocation and succeed on a second:

```bash
solari-workflow run-codex-gate
```

**Expected**: the runner retries exactly once automatically (research.md
§0.6) and succeeds; `.ai-runs/.block-state.json` never shows more than one
retry recorded. Now configure the stub to fail **twice**:

```bash
solari-workflow run-codex-gate
```

**Expected**: exit code `2`, `state: BLOCKED_MANUAL`,
`last_safe_stage: IMPLEMENTATION_COMPLETE` (the gate never got far enough
to reach `GATE_PASSED`). Fix whatever caused the simulated failure (in
this drill, just point the stub back at its normal success behavior), then:

```bash
solari-workflow resume
```

**Expected**: `resume` revalidates branch/HEAD, does **not** re-invoke
`run-claude` (implementation was already `IMPLEMENTATION_COMPLETE`), and
proceeds directly to the gate stage, producing a normal `PASS` result.

## Scenario 8 — Unexpected staged changes are refused, not repaired

After `start-block`, before running anything else, manually stage an
unrelated file the workflow didn't produce:

```bash
echo "manual" > unrelated.txt && git add unrelated.txt
solari-workflow run-codex-gate
```

**Expected**: exit code `2`, a message naming `unrelated.txt` as
unexpectedly staged (research.md §0.8), and — checked directly —
`git diff --cached --name-only` still lists `unrelated.txt` afterward
(the runner never ran `git reset` or any other index-mutating command on
its own). Resolve manually (`git reset unrelated.txt` or commit it,
whichever the human intends), then re-run `run-codex-gate` successfully.

## Where to look when something fails

- `.ai-runs/*-prompt.md` / `*-result.md` — full audit trail for every
  execution and gate (research.md §8).
- `.ai-runs/.lock` — present only while an execution owns the working
  tree; its JSON payload names the current owner (research.md §7).
- `.ai-runs/.block-state.json` — the current block's state, last safe
  stage, retry count, and last recorded gate/Fingerprint X (research.md
  §0.7); safe to delete and let the next `start-block`/`resume` rebuild
  from `.ai-runs/` history if it looks wrong.
