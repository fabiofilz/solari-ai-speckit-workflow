# Contract: `solari-workflow` CLI Interface

Every subcommand here is issued by the **Orchestrator** (ChatGPT), never
directly by the User and never by Claude or Codex (research.md §0.0). The
User requests work in plain language ("implement the next block"); the
Orchestrator is what translates that into these CLI invocations. The
runner exposes its behavior **only** as discrete subcommands, one per
lifecycle stage (research.md §0.4). There is no "run everything" command
and no "run N blocks" command — this absence is the technical enforcement
of Controlled Automation (constitution Principle VII) and of
`requested_block_count` living entirely outside the runner (research.md
§0.3). Every subcommand:

- takes explicit flags — nothing is inferred silently (session mode,
  model, effort, block boundaries are always supplied by the
  Orchestrator, never guessed, and never requested piecemeal from the
  User — research.md §0.3);
- acquires the run lock (research.md §7) for its own duration only and
  releases it on exit, success or failure;
- exits `0` on success, non-zero on any failure, with a human-readable
  message on stderr and (where applicable) a machine-checkable status
  written to the relevant `.ai-runs/*-result.md`;
- updates `.ai-runs/.block-state.json` (research.md §0.7) on entry and
  exit, so a resume after an interruption always has an accurate picture
  of the last safely completed stage;
- never writes to `.ai-workflow.toml` (read-only from the runner's
  perspective, per config ownership rules), except `init` (below).

## Retry & failure handling (applies to every subcommand)

Any subprocess invocation of `git`/`claude`/`codex`/`uv` that fails with a
plausibly transient, operational cause (process failed to launch, a
short-lived resource contention, a non-content-related non-zero exit) gets
**exactly one** automatic retry before the subcommand gives up. Every
other failure class (invalid config, Git ambiguity, an unexpected staged
state, a persistent QA `FAIL`, an architectural-decision stop, a
destructive-operation requirement, an unsupported CLI capability) **never**
auto-retries — the subcommand stops immediately with `state:
BLOCKED_MANUAL` (or `ARCHITECTURAL_DECISION_REQUIRED` /
`QA_REMEDIATION_REQUIRED` where more specific) recorded in block-state
(research.md §0.6/§0.7). Exit code `2` is reserved across all subcommands
for "stopped for manual/orchestrator intervention," distinct from `1`
("ran to completion but the substantive result was negative," e.g. a gate
`FAIL` or a checkpoint block).

## `solari-workflow init`

Scaffolds `.ai-workflow.toml` for a project that doesn't have one yet
(research.md §17). Prompts for/accepts values the orchestrator supplies;
never guesses project-specific values. Ensures `.ai-runs/` and the lock/
block-state file paths are present in `.gitignore`. Does not touch Spec
Kit artifacts — Spec Kit's own `specify init` / `/speckit.*` commands
remain a separate step (FR-013).

```
solari-workflow init --project-name <name> --stack python|node|go|rust|other ...
```

Exit codes: `0` success; `1` config already exists (refuses to overwrite
without an explicit `--force`, and even then shows a diff before writing).

## `solari-workflow readiness-check`

Runs the Project Readiness Gate (research.md §11) against the current
working tree. Read-only — makes no Git or filesystem changes, and does not
touch block-state.

```
solari-workflow readiness-check
```

Exit codes: `0` = overall `PASS`; `1` = overall `FAIL` or `INCOMPLETE`
(readiness overall status is `FAIL\|INCOMPLETE\|PASS`, precedence
`FAIL > INCOMPLETE > PASS` — data-model.md's "Readiness Gate Result").
Output: the full `CheckResult` list (including `SKIPPED` entries) to
stdout, human-readable; never a partial/truncated list.

## `solari-workflow start-block`

**Runner-owned branch creation** (research.md §0.1/§0.10/§13) — the
**only** code path that creates a development branch. Requires an
orchestrator-supplied block definition; never infers one from `tasks.md`.

```
solari-workflow start-block --first-task T091 --last-task T099 --block-name "Validation Pipeline"
```

Sequence: (1) run the readiness gate; (2) check the real staging-area
precondition (research.md §0.8) — refuses if the real index already has
staged content unrelated to this workflow; (3) verify no branch matching
the derived name already exists unexpectedly; (4) deterministically derive
`T091-ValidationPipeline` (research.md §16) and create + check out the
branch from the current `main`; (5) initialize `.ai-runs/.block-state.json`
with `state: RUNNING`, `last_safe_stage: BRANCH_CREATED`.

Exit codes: `0` = branch created; `1` = readiness `FAIL`; `2` = staging
precondition violated or branch-name collision (manual intervention).

## `solari-workflow run-claude`

Invokes the `claude` CLI for one implementation/remediation execution
(research.md §9) **against a branch `start-block` already created** —
`run-claude` verifies the current branch matches the active block-state's
`branch_name` and refuses (rather than creating one) if it doesn't.

```
solari-workflow run-claude \
  --model <model> --effort <effort> \
  --session-mode NEW|CONTINUE [--session-id <id>] \
  --purpose "<free text>"
```

`--session-mode CONTINUE --session-id <id>` is only valid when the
orchestrator explicitly supplies it — there is no default resume behavior
(FR-009). Produces one `.ai-runs/` prompt/result pair and parses Claude's
trailing `STATUS:` marker (research.md §9); missing/unparseable → treated
as `BLOCKED`. Updates block-state: `STATUS: COMPLETE` →
`last_safe_stage: IMPLEMENTATION_COMPLETE`, `state: RUNNING`;
`ARCHITECTURAL_DECISION_REQUIRED` → `state:
ARCHITECTURAL_DECISION_REQUIRED` (no retry, ever); `BLOCKED` or an
operational launch failure → the retry-then-`BLOCKED_MANUAL` path above.

**Claude never executes a Git command during this invocation** (research.md
§0.11 — the complete prohibited list is `status`, `diff`, `add`, `commit`,
`checkout`, `switch`, `branch`, `merge`, `rebase`, `reset`, `stash`, `tag`,
`push`, and branch deletion). Claude only reads/writes project files and
runs non-Git project commands (test/lint/build); the branch it operates on
was already created and checked out by `start-block` before this
invocation began, and the Runner's own Candidate Tree construction (§12)
is what turns Claude's on-disk edits into a reviewable state afterward —
Claude itself never finalizes anything via Git.

Exit codes: `0` = `STATUS: COMPLETE`; `1` = `ARCHITECTURAL_DECISION_REQUIRED`
(stop, awaiting an orchestrator architecture decision); `2` = `BLOCKED` or
an operational failure after the single retry (manual intervention).

## `solari-workflow run-codex-gate`

Invokes the `codex` CLI for one independent, read-only gate (research.md
§10, §12). Always a NEW Codex session — there is no `--session-mode` flag
for this subcommand at all.

```
solari-workflow run-codex-gate
```

Sequence: (1) verify current branch matches block-state's `branch_name`;
(2) check the real staging-area precondition (research.md §0.8) — fail
fast before spending a Codex session; (3) build the Candidate Tree and
Fingerprint X (research.md §12), and — if the review benefits from a diff
view — render one from the Candidate Tree via the Runner's own `git`
access, to include as context; (4) invoke `codex` (with that context, if
prepared); (5) immediately rebuild the Candidate Tree and compare — a
changed `candidate_tree_oid` is a hard failure (`Codex gate modified the
working tree`) regardless of Codex's own reported result; (6) parse
Codex's structured result (`contracts/codex-gate-result-contract.md`) —
missing/unparseable → `FAIL`, never `PASS`; (7) record the full result
plus Fingerprint X into `.ai-runs/*-result.md` and
`.ai-runs/.block-state.json` (`last_gate`, `last_safe_stage: GATE_PASSED`
only on an eligible `PASS`).

**Codex never executes a Git command during this invocation** (research.md
§0.12 — the same prohibited list as Claude's, plus never staging,
committing, merging, or tagging regardless of its own verdict). Any
repository context Codex needs is prepared by the Runner in step (3) above
and handed to it as plain text — Codex never runs `git diff`/`git status`
itself to obtain it.

Exit codes: `0` = `PASS` with no findings at/above
`checkpoint.blocking_severities`; `1` = `FAIL` or unparseable result or a
Codex-modified-tree detection (`state: QA_REMEDIATION_REQUIRED`); `2` =
precondition failure (staged-area conflict, wrong branch, lock contention,
tool not found, or an operational failure after the single retry).

## `solari-workflow checkpoint`

Runs the checkpoint flow (research.md §13) — the runner's own checkpoint
commit, merge, tag, and retention. Requires the real staging area to be
clean and the branch to have a recorded `PASS` gate with a matching
Fingerprint X.

```
solari-workflow checkpoint
```

Sequence: branch verification → real staging-area precondition (§0.8) →
gate `PASS` verification → Fingerprint recheck (a mismatch here is the one
place the exact string below is required) → stage Candidate Tree into the
real index (`git read-tree`) → verify staged tree → checkpoint commit
(`git commit-tree` + `git update-ref`) → verify commit tree → switch to
`main` → `git merge --no-ff` → verify merge → annotated tag → branch
retention → block-state `state: COMPLETED`. Full step-by-step detail and
the required invariant chain are in research.md §13.

Exit codes: `0` = checkpoint created (merge + tag); `1` = blocked — a
fingerprint mismatch prints **exactly**
`CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE`; a gate that isn't
an eligible `PASS` prints the specific reason; `2` = precondition failure
(wrong branch, staged-area conflict, lock contention, a merge conflict —
never auto-resolved).

Never pushes (research.md §0.2) and never starts another block afterward
— the next block always requires a fresh `start-block` invocation from the
orchestrator (research.md §0.3/§0.10).

## `solari-workflow resume`

Revalidates and continues an interrupted block from its `last_safe_stage`
(research.md §0.7) — never blindly re-runs completed work. Issued by the
Orchestrator only after the **User** has authorized resuming (having
resolved whatever caused the stop) — the Orchestrator does not decide on
its own that it's safe to continue.

```
solari-workflow resume
```

Refuses unless `.ai-runs/.block-state.json`'s `state` is `BLOCKED_MANUAL`
or `ARCHITECTURAL_DECISION_REQUIRED`. Revalidates the recorded branch/HEAD
(and, if `last_safe_stage` was `GATE_PASSED`, the recorded Fingerprint X
against a freshly rebuilt Candidate Tree — exactly as `checkpoint` itself
would). On success, sets `state: READY_TO_RESUME` and immediately
continues at the first incomplete stage after `last_safe_stage` (e.g.
`IMPLEMENTATION_COMPLETE` resumes at `run-codex-gate`, never re-invoking
`run-claude`). On failure, stops with an explanation — it never restarts a
stage from zero on its own judgment.

Once the resumed block completes, the multi-block authorization it was
part of is finished (research.md §0.3/§0.7) — the Orchestrator does not
automatically continue into any further blocks that were originally
requested alongside it; that requires a new User request.

Exit codes: `0` = resumed and the next stage completed; `1` = revalidation
failed (state remains `BLOCKED_MANUAL`, with the specific reason); `2` =
called when no resumable state exists.

## `solari-workflow retention`

Runs branch retention (research.md §15) standalone, in case an operator
wants to re-apply it without a fresh checkpoint (e.g. after manually
adjusting `[branch_retention]` config).

```
solari-workflow retention [--dry-run]
```

`--dry-run` lists deletion candidates without deleting anything. Exit code
`0` regardless of how many branches were pruned (pruning zero branches is
not a failure).

## `solari-workflow status`

Read-only convenience command: reports current lock ownership (if any),
the full contents of `.ai-runs/.block-state.json` (if a block is active),
and whether a checkpoint would currently be blocked. Never mutates
anything, and is always safe to run regardless of state.

```
solari-workflow status
```

## No `push` subcommand in v1

Per `[git.push].mode = "manual"` (research.md §0.2), pushing requires the
**User's** explicit authorization and is an out-of-band human action in
v1 — there is deliberately no `solari-workflow push` command to design or
ship. The User pushes themselves, outside this CLI, once satisfied. A
future workflow version may add an explicit push command alongside a new
accepted `mode` value, invoked by the Orchestrator only after the User
authorizes it — never by Claude or Codex, under any circumstances — without
reshaping the config table.
