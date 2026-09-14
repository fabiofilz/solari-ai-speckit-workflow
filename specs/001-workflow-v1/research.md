# Phase 0 Research: AI Spec Kit Workflow v1 Runner

Each section resolves one open technical question from the spec (FR-038) and
the planning brief. Format: **Decision / Rationale / Alternatives
considered**. Priority markers — **MUST** (v1-required), **SHOULD**
(v1-if-inexpensive), **DEFERRED** (explicitly post-v1) — are called out
where relevant.

**§0 supersedes and corrects** the original planning pass's checkpoint
mechanics (a prior draft assumed Codex reviews an already-committed clean
tree; that assumption is wrong and is replaced here). §12 and §13 below
are rewritten to match §0; every other section is unaffected unless it
cross-references the corrected design.

---

## 0. Canonical responsibility model, block-execution control, remediation, retry, and resume — MUST

This section formalizes the final v1 responsibility boundaries and the
control model built around them. It is intentionally the smallest
machinery that satisfies every constraint below — see the "kept simple"
note at the end of each subsection for what was deliberately *not* added.

### 0.0 The six components — User is not the Orchestrator

**The User and ChatGPT/the Orchestrator are two distinct actors.** Earlier
drafts of this plan sometimes wrote as if "the orchestrator" and "the
person typing" were interchangeable; they are not, and nothing below may
blur that line again.

```
User            → REQUESTS / AUTHORIZES
ChatGPT/Orchestrator → DECIDES / INSTRUCTS
Python Runner   → EXECUTES / VERIFIES / GUARANTEES
Claude Code     → IMPLEMENTS
Codex           → VALIDATES
Spec Kit        → DEFINES THE FORMAL CONTRACT
```

- **User** owns human intent and authorization: requests work ("implement
  the next block" / "execute the next 2 blocks"), authorizes how many
  blocks a run may cover, provides manual intervention when the workflow
  stops, authorizes any operation that needs explicit human approval
  (including an eventual push, §0.2), and watches externally for concerns
  the workflow itself has no visibility into (e.g. token/context budget).
  The User is **not** required to manually coordinate every internal
  workflow phase — branch creation, invoking Claude, invoking Codex,
  checkpointing, merging, and tagging are internal mechanics the User
  never has to ask for individually (§0.3).
- **ChatGPT/Orchestrator** interprets the User's request and makes every
  engineering/orchestration decision: architecture, coherent Spec Kit
  task-block boundaries, which block comes next, block name, tool/model/
  effort/session-mode selection, remediation strategy, architectural
  exceptions, and whether a blocked workflow may resume. The Orchestrator
  **never** manipulates Git directly — it only instructs the Runner.
- **Runner** (the Python CLI) executes deterministic operations the
  Orchestrator requests and verifies their pre/postconditions; it is the
  **exclusive** interface between the AI-assisted workflow and Git (§0.4).
  It is not the architect and never makes an engineering decision.
- **Claude Code** implements: reads/writes project files within the
  assigned scope, self-validates, remediates findings — entirely without
  Git (§0.11).
- **Codex** validates: independently, read-only, entirely without Git
  (§0.12).
- **Spec Kit** defines the formal contract (specification, clarification,
  plan, task decomposition) that the Orchestrator, Claude, Codex, and the
  Runner all work against, but never chooses block boundaries or Git
  topology itself.

**Canonical relationship**:

```
User → requests/authorizes
ChatGPT/Orchestrator → decides/instructs
Runner → executes/verifies
  ├─ Claude Code → implements (no Git)
  └─ Codex → validates (no Git)
```

This distinction, and the "no Git" boundary on Claude and Codex, is a v1
architectural invariant — not a preference to be revisited per project.

### 0.1 Actor responsibility table

| Actor | Owns | Never does |
|---|---|---|
| **User** | Requesting work; authorizing `requested_block_count` (§0.3); providing manual intervention and authorizing resume (§0.7); authorizing an eventual push (§0.2); watching externally for concerns the workflow can't see (e.g. budget/time). | Does not need to individually request branch creation, Claude/Codex invocation, checkpoint, merge, or tag — those are internal mechanics the Orchestrator coordinates on the User's behalf once a block count is authorized. |
| **ChatGPT/Orchestrator** | Interpreting the User's request; architecture; task-block boundaries; block name; tool/model/effort/session-mode choice; remediation strategy; architectural exceptions; whether a `BLOCKED_MANUAL` workflow may resume (pending the User's own authorization to resume, §0.7); workflow-policy changes. | Does not invoke `git` directly, ever; expresses every decision as an explicit Runner invocation. Never causes execution beyond what the User authorized (§0.3). |
| **Spec Kit** | Specification, clarification, plan, task decomposition (the formal contract). | Does not choose block boundaries or Git topology — it only supplies the task list the Orchestrator selects a range from; does not implement code, approve QA, or choose models/session strategy. |
| **Claude Code** | Reading/modifying project files within assigned scope; implementation-related tests/docs; self-validation; remediation of valid QA findings — entirely within the working tree, entirely without Git (§0.11). | Never executes **any** Git command (§0.11) — no commits, no staging, no branch creation, nothing. Never performs checkpoint staging, the checkpoint commit, merge, tag creation, branch retention, or push. Never creates the block branch or the next block's branch. |
| **Codex** | Independent, read-only review of the candidate implementation state the Runner exposes; runs/inspects approved non-Git validation commands; reports PASS/FAIL with severity-classified findings; a PASS is bound to a specific Fingerprint the Runner supplies (§0.9/§12) — entirely without Git (§0.12). | Never executes **any** Git command (§0.12). Never modifies implementation; never gets to unilaterally waive its own findings (research.md §10's severity-policy backstop still applies). |
| **Runner** | The **exclusive** interface between the workflow and Git (§0.4): repository/state inspection, the orchestrator-authorized block branch's creation, Candidate Tree construction, Fingerprint compute/verify, checkpoint staging, the checkpoint commit, `git merge --no-ff`, post-merge verification, annotated tag creation, safe branch retention, and (only when explicitly authorized) push. Enforces deterministic invariants and fails closed on ambiguous state (§0.1's "Runner must not decide" list, below). | Never decides task-block boundaries, which task comes next, architecture, whether a QA finding is architecturally acceptable, model/effort/session-mode choice, whether requirements change, or whether execution continues beyond what the User authorized. Never pushes without a separate, explicit authorization. |

**Kept simple**: this table only names the boundary; it does not introduce
a bespoke permissions-enforcement mechanism beyond what already exists —
the run lock ensures only one actor's process is ever active against the
tree at once (research.md §7), the CLI never exposes a "Claude/Codex,
please touch Git" capability at all (§0.11/§0.12 make this explicit), and
`.ai-workflow.toml` ownership (contracts/config-schema.md) already
prevents Claude/Codex from silently changing policy.

**Runner-enforced deterministic invariants (illustrative, not exhaustive)**
— the Runner's whole job is executing what the Orchestrator asked *and*
verifying it was safe to; every one of these is a STOP, never a guess:

| Condition | Runner behavior |
|---|---|
| Expected repository not present | STOP |
| Unexpected current branch | STOP (§13 step 1) |
| Unexpected staged state | STOP (§0.8) |
| Fingerprint changed after the gate | STOP — exactly `CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE` (§12, §13) |
| Codex did not produce an eligible PASS | Checkpoint forbidden (§0.5) |
| A requested Git operation itself fails (e.g. merge conflict) | Report the failure; never auto-resolve (§13) |
| A transient operational failure | Perform the single allowed retry, then STOP if it recurs (§0.6) |
| Any other unsafe/ambiguous state | STOP for Orchestrator/User intervention (`BLOCKED_MANUAL`, §0.7) |

### 0.2 Push policy (v1: manual only)

Push is never automatic in v1. The `git.push_allowed` boolean from the
original config draft is replaced with a small, forward-compatible policy
table:

```toml
[git.push]
mode = "manual"
```

- v1 recognizes **only** `mode = "manual"` — an unrecognized value is a
  hard config-validation failure (this is a known field with a fixed
  enum, not an unknown key — see the unknown-field leniency rule in §5,
  which does not apply to invalid *values* of a recognized field).
- No `solari-workflow` subcommand in v1 pushes anything, ever. A future
  workflow schema version may add modes (e.g. a prompted or
  policy-gated automatic push) without redesigning this table — only
  `mode`'s accepted value set grows; the table shape itself does not
  change. v1 deliberately does not design that automation now.
- Local branch creation, local checkpoint commit, and local
  `merge --no-ff` all remain runner-managed regardless of push mode —
  push is the one step that stays out-of-band for v1. Two sub-cases, both
  requiring the **User's** explicit authorization (not the Orchestrator's
  own judgment — push crosses a "share this externally" line that stays
  with the human per this plan's opening framing): if a future v1 CLI
  revision exposes an explicit `solari-workflow push` capability gated on
  that authorization, the Orchestrator may instruct the Runner to run it;
  otherwise (the actual v1 CLI surface, §0.4 — no `push` subcommand exists
  yet) the User performs the push themselves, entirely outside the
  automated workflow. Either way, **Claude and Codex never push, under any
  circumstances** — pushing isn't a capability either of them has (§0.11,
  §0.12).
- Future workflow versions may introduce an automatic push policy once the
  process is proven reliable; v1 does not design or implement that
  automation.

### 0.3 User-authorized block execution count

**Decision**: `requested_block_count` originates with the **User**
("implement the next block" → `requested_block_count = 1`; "execute the
next 2 blocks" → `requested_block_count = 2`) and is **interpreted by the
Orchestrator** — the Runner's own CLI has no concept of a block count at
all, and needs none. Every Runner subcommand (§0.4) acts on exactly one
already-identified block per invocation and then stops unconditionally,
regardless of what was requested — the Runner literally cannot start block
N+1 on its own because nothing in its CLI surface lets it choose a next
block (§0.1: only the Orchestrator selects block boundaries, and only in
response to a User request).

Concretely: "execute the next 2 blocks" means the User authorizes 2, the
Orchestrator determines block 1's task range and coordinates that block's
full command sequence to completion (§0.4) *without* asking the User to
individually approve branch creation, Claude invocation, Codex invocation,
or checkpointing — those are internal mechanics — and — only then, and
only because 2 were authorized — determines block 2's task range and
coordinates that sequence too. The Runner is invoked repeatedly (several
times per block, once per block boundary) and never knows "2 were
authorized"; that count is tracked entirely by the Orchestrator, on the
User's behalf.

**A User-authorized multi-block request is consumed by an interruption**:
if block 1 of a 2-block authorization hits `BLOCKED_MANUAL` or
`ARCHITECTURAL_DECISION_REQUIRED` (§0.7), the *entire* current execution
request stops — the Orchestrator does not queue "block 2 is still owed"
across the interruption. Once the User authorizes resume and block 1
eventually completes, that original 2-block request is considered
finished; starting block 2 requires a **new** User request, even though
the User originally said "2." This keeps the resume model simple (§0.7)
and keeps the User, not an implicit counter, in control of what happens
after an interruption.

**Kept simple**: no `--block-count` flag, no token budget, no max-
invocation counter, no "automation level" setting anywhere in the Runner.
The invariant "never start block N+1 after the requested count is reached"
holds trivially because the Runner never starts *any* next block on its
own, ever, independent of any count — and the "consumed by interruption"
rule above means the Orchestrator never needs to persist a remaining-count
across a stop/resume cycle either.

### 0.4 Complete block lifecycle → CLI command sequence

One block's full chain, matching the required stopping points already in
the ratified spec (FR-010/FR-011 — this correction pass does not relax
those; each arrow below is a **separate** Runner invocation, with control
returning to the Orchestrator between them). The User's role is entirely
upstream of this sequence (authorizing `requested_block_count`, §0.3) and
downstream only if something stops (§0.7) — the User is not shown as a
per-step participant because the whole point of this chain is that they
don't have to be:

```
User: "implement the next block" (or "the next N")   [§0.3]
Orchestrator: select task range + block name
  → solari-workflow start-block           (Runner: preconditions + branch creation; §13)
Orchestrator: invoke Claude (model/effort/session-mode it chose)
  → solari-workflow run-claude            (Runner invokes Claude; Claude: implementation
                                            + self-validation, no Git at all; §0.11, §9)
Orchestrator: request the gate
  → solari-workflow run-codex-gate        (Runner: Candidate Tree + Fingerprint X, invokes
                                            Codex with Runner-supplied diff evidence;
                                            Codex: validation, no Git at all; §0.12, §10, §12)
      ├─ FAIL (blocking findings) → Orchestrator decides remediation (§0.5)
      └─ PASS (no blocking findings) → Orchestrator requests checkpoint
  → solari-workflow checkpoint            (Runner: re-verify, stage, commit, merge, tag,
                                            retain; §13)
Orchestrator: block complete, reports to User.
  If more blocks remain under the current authorization (§0.3):
    Orchestrator selects the next block and the sequence repeats from
    start-block.
  Otherwise: stop and wait for the User's next request.
```

No step in this chain is fused into a "do everything" command — the
absence of such a command is what makes FR-011's stopping points a
technical fact rather than a documentation promise (plan.md's Constitution
Check, Principle VII). Note that **Claude and Codex never appear as
callers of `git` anywhere in this chain** — every Git-touching line above
names the Runner, never Claude or Codex (§0.11, §0.12).

### 0.5 QA remediation behavior

A Codex `FAIL` caused by real implementation defects is normal workflow
operation, not an operational error (§0.6 draws that distinction sharply).
Expected shape:

```
run-codex-gate → FAIL (findings)
  → orchestrator invokes run-claude again (same branch, CONTINUE or NEW
    per orchestrator's own choice — FR-009) to remediate
  → run-claude's own self-validation
  → run-codex-gate again — a brand-new Codex session (never CONTINUE for
    Codex, §10) — builds a fresh Candidate Tree/Fingerprint and re-gates
  → PASS or another FAIL; repeat until PASS or an architectural-decision
    stop (below)
```

- Remediation **never** creates a new branch — it stays on the same block
  branch (constitution Principle III/FR-026), even when the fix touches
  code from an earlier block.
- A block is **never** checkpointed while the latest gate result for its
  branch carries any finding at or above `[checkpoint].blocking_severities`
  (research.md §10/§14) — `checkpoint` re-derives this from the recorded
  gate result every time, it is not a one-time check that could go stale.
- If remediation surfaces an out-of-scope architectural decision, Claude
  stops and reports (constitution Principle I/FR-003; §9's status
  contract gives this a parseable shape) rather than deciding it — the
  runner records state `ARCHITECTURAL_DECISION_REQUIRED` (§0.7) and halts;
  it never guesses an architecture change to keep moving.

**Why this isn't "autonomous infinite progression"**: each remediation
round requires a genuinely new orchestrator decision to invoke `run-claude`
again — the runner never loops `run-claude`/`run-codex-gate` on its own
without that external decision point in between (this is the same FR-011
stopping-point requirement, applied literally to the remediation cycle).

### 0.6 Retry vs. stop-for-manual-intervention

**Decision**: at most **one** automatic retry, and only for a narrow class
of *operational/transient* failures — never for anything that reflects a
real decision, conflict, or persistent condition.

| Failure class | Example | Automatic retry? |
|---|---|---|
| Transient process failure | `claude`/`codex`/`git`/`uv` failed to launch, a transient non-zero exit with no stable cause, a short-lived resource contention | **Yes — exactly one retry**, then stop if it fails again |
| Configuration error | Invalid `.ai-workflow.toml`, missing required field | **No** — stop immediately |
| Git ambiguity | Merge conflict, unexpected branch/HEAD state, staging-area precondition violation (§0.8) | **No** — stop immediately |
| Architecture conflict | Claude reports `ARCHITECTURAL_DECISION_REQUIRED` | **No** — stop immediately |
| Persistent QA finding | Codex `FAIL` (even after remediation) | **No** — this is normal remediation flow (§0.5), not a retryable error, and is never auto-retried as if it were transient |
| Destructive-operation requirement | Any step that would need a force-delete, forced push, or history rewrite to proceed | **No** — stop immediately, never performed automatically at all |
| Unsupported CLI capability | Installed `claude`/`codex` CLI lacks a requested flag/mode | **No** — stop immediately (research.md §9) |

The retry count is tracked **per stage**, reset whenever a new stage
begins (§0.7's `retry_count_current_stage`), and is a fixed constant
(`1`) — **not** a configurable value in `.ai-workflow.toml`, per the
instruction to keep this machinery minimal rather than building a
general-purpose retry/backoff system.

### 0.7 Persistent resume model

**Decision**: one small ephemeral-but-load-bearing state file per project,
**`.ai-runs/.block-state.json`**, tracks exactly enough to resume an
interrupted block safely without redoing completed work:

```json
{
  "branch_name": "T091-ValidationPipeline",
  "first_task": "T091", "last_task": "T099", "block_name": "Validation Pipeline",
  "state": "RUNNING",
  "last_safe_stage": "IMPLEMENTATION_COMPLETE",
  "retry_count_current_stage": 0,
  "last_gate": null,
  "updated_at": "2026-09-12T00:00:00Z"
}
```

**State vocabulary** (deliberately small — this is not a general workflow
engine):

- `RUNNING` — a stage is actively executing (the run lock, §7, is held).
- `QA_REMEDIATION_REQUIRED` — the latest gate is `FAIL`; awaiting an
  Orchestrator decision to remediate.
- `RETRYABLE_ERROR` — a transient failure occurred and the single
  automatic retry (§0.6) is in flight or about to be attempted.
- `BLOCKED_MANUAL` — the retry failed, or a non-retryable failure
  occurred; **the entire current execution request stops here** — no
  further block in the same multi-block authorization begins (§0.3) —
  and requires User-authorized, Orchestrator-initiated intervention
  before anything resumes.
- `ARCHITECTURAL_DECISION_REQUIRED` — Claude stopped for an out-of-scope
  decision (§0.5); requires an Orchestrator architectural decision (which
  may itself require going back to the User), not merely a retry.
- `READY_TO_RESUME` — revalidation (below) has confirmed it is safe to
  continue; set only by the `resume` command itself, never by anything
  else, and only after the User has authorized the resume and the
  Orchestrator has instructed the Runner to attempt it.
- `COMPLETED` — the block finished (checkpoint created); the file is
  effectively inert until the next `start-block` overwrites it for a new
  block.

**`last_safe_stage`** values: `BRANCH_CREATED`, `IMPLEMENTATION_COMPLETE`,
`CANDIDATE_TREE_BUILT`, `GATE_PASSED`, `CHECKPOINT_COMPLETE` — the last
stage the runner is *certain* completed correctly, i.e. the point resume
continues from rather than restarting from scratch.

**Resume flow** (`solari-workflow resume`, `contracts/cli-interface.md`).
Per §0.0's canonical chain, this always starts with the User, not the
Runner deciding on its own that it's safe to continue:

0. **User** authorizes the resume (having resolved whatever caused the
   stop); **Orchestrator** evaluates the situation and instructs the
   Runner to attempt it.
1. Runner reads `.ai-runs/.block-state.json`. If state is not
   `BLOCKED_MANUAL` or `ARCHITECTURAL_DECISION_REQUIRED`, refuse — resume
   is only meaningful from a blocked state.
2. **Revalidate** repository/workflow state from scratch rather than
   trusting the file blindly: confirm the recorded branch still exists and
   is checked out, confirm `HEAD` matches what's expected for
   `last_safe_stage`, and — if `last_safe_stage` is `GATE_PASSED` — confirm
   the recorded gate's Fingerprint X (§0.9/§12) still matches a freshly
   recomputed Candidate Tree, exactly as `checkpoint` itself would.
3. If revalidation succeeds: set state to `READY_TO_RESUME`, then
   immediately continue at the first incomplete stage after
   `last_safe_stage` — e.g. `last_safe_stage = IMPLEMENTATION_COMPLETE`
   resumes at the gate stage, never re-running Claude's implementation.
4. If revalidation fails (e.g. the branch was deleted, or the fingerprint
   no longer matches): resume itself stops with an actionable explanation
   — it never silently restarts a stage from zero on the Orchestrator's
   behalf.

**After the interrupted block completes**: per §0.3, the original
multi-block authorization that was in flight when the stop happened is
now finished, whether or not it originally covered more than the one
block that just resumed — the Orchestrator does not resume "and then
keep going" through a remembered remaining count. Any further block
requires a new, explicit User request.

**Fallback / kept simple**: `.ai-runs/.block-state.json` is a fast-path
cache, not the sole source of truth — if it is lost or looks inconsistent,
`resume`/`status` can reconstruct enough of it by inspecting the current
branch, the current Git state, and the most recent `.ai-runs/*-result.md`
records (research.md §8), since those are the permanent, immutable audit
trail. No database, no generic workflow-engine state store, and no
attempt to model states/transitions beyond the seven listed above.

### 0.8 Real staging-area precondition

**Decision**: before any workflow-managed sequence reads or writes Git
state that assumes a known-clean starting point, the runner checks that
the **real index** (not the working directory) has no staged differences
from `HEAD` — `git diff --cached --quiet` (research.md §6's subprocess
wrapper; exit `0` = clean). This is checked at three points, reusing one
function:

1. `start-block` — before creating the branch, as an early sanity check.
2. `run-codex-gate` — before building the Candidate Tree, to fail fast
   (before spending a Codex session) if the real index has drifted.
3. `checkpoint` — immediately before the runner stages the approved
   Candidate Tree into the *real* index (§13) — this is the load-bearing
   check: staging into an index that already has unrelated content would
   silently discard that content, which is exactly the destructive
   scenario this precondition exists to prevent.

**Why this doesn't require the working directory to be clean**: the
Candidate Tree (§0.9/§12) is built from a *temporary* index populated
directly from the working directory's current on-disk content — it never
reads the real index at all during construction. Claude's uncommitted
edits, staged or not, are exactly what's expected to exist there; only the
*real* index's relationship to `HEAD` matters for this precondition,
because the real index is the only piece of Git state the runner will
later overwrite (`git read-tree`, §13).

**On violation**: stop, do not touch the index, and explain the conflict
(e.g. `WORKFLOW BLOCKED — UNEXPECTED STAGED CHANGES`, naming the staged
paths from `git diff --cached --name-status`) with the instruction to
resolve manually (a human running `git status`/`git reset`/`git commit`
themselves, outside the workflow — never Claude, per §0.11). The runner
never runs `git reset`, `git stash`, or any other index-mutating command
to "fix" this on its own — no destructive auto-recovery, matching the same
principle already applied to lock-file staleness (research.md §7).

**Consequence of §0.11 (Claude never touches Git)**: in normal operation
this precondition is now close to *trivially* satisfied at every check
point — since Claude never runs `git add`, the real index simply has
nothing to drift from `HEAD` with, ever, as a matter of course. A
violation is therefore unambiguous evidence of an out-of-band manual
action (someone ran `git add` themselves outside the workflow), not a
routine condition the workflow needs to negotiate around — which is
exactly the fail-closed posture this precondition is meant to provide.

### 0.9 Candidate Tree & fingerprint — see research.md §12 for the full mechanism

The core correction: Codex reviews the **uncommitted** state a block is
proposing, not an already-committed clean tree. The Candidate Tree is
computed via an isolated temporary Git index (never the real one) and its
`git write-tree` OID, together with the current `HEAD` OID and the block's
identity, **is** the fingerprint the runner binds a Codex PASS to. Full
mechanism, exact invariant chain, and the required
`CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE` message are in
research.md §12 (fingerprint) and §13 (checkpoint flow), which fully
supersede the prior draft's clean-committed-tree assumption.

**Consequence of §0.11**: since Claude never commits, `HEAD` (the block
branch's tip) stays exactly at the commit `start-block` created it from
for the *entire* block — through implementation, self-validation, any
number of remediation rounds, and gating — and only ever moves once, at
`checkpoint`, via the Runner's own `commit-tree`/`update-ref` (§13). This
makes the "confirm `head_oid` hasn't moved" half of every fingerprint
recheck close to a formality in normal operation (it can only move via a
Runner-issued Git command), while the `candidate_tree_oid` half is where
all of Claude's actual implementation progress is captured, gate to gate.

### 0.10 Branch creation & next-block non-invention

Already stated structurally in §0.1/§0.3/§0.4: `start-block` is the
**only** code path that creates a development branch, and it always takes
an Orchestrator-supplied `--first-task/--last-task/--block-name` triple —
it never infers a task range from `tasks.md` on its own. Claude does not
create it, Codex does not create it, and the User does not need to create
it manually (§0.11/§0.12). After `checkpoint` completes a block, no runner
command exists that starts another block unprompted; the next
`start-block` invocation always requires a fresh, explicit
Orchestrator-supplied block definition, itself downstream of a User
request (§0.3).

### 0.11 Claude Code — absolute Git prohibition

**Decision**: during workflow-managed implementation, Claude Code may read
project files, modify/create project files (including tests and
implementation-related docs), and execute project test/lint/build commands
that do not mutate Git — nothing more. Claude Code **MUST NOT** execute any
Git command, without exception. This explicitly includes (not merely by
omission of permission, but as a named prohibition): `git status`, `git
diff`, `git add`, `git commit`, `git checkout`, `git switch`, `git
branch`, `git merge`, `git rebase`, `git reset`, `git stash`, `git tag`,
`git push`, any branch deletion, and any checkpoint-related operation.
Claude's responsibility begins and ends at the content of the working
tree; it never has a reason to run `git` at all, because:

- the branch it works on already exists and is already checked out —
  created by `start-block` (§0.10) before Claude is ever invoked;
- it never needs to know "what changed" via `git diff` — if repository/Git
  context is useful to Claude at all (e.g. during remediation, to know
  which files a Codex finding referenced), the Orchestrator/Runner supply
  that context directly in the prompt, not by granting Claude a `git`
  command;
- it never commits, stages, or otherwise finalizes its own work — the
  Runner's Candidate Tree construction (§12) captures whatever is on disk
  regardless of Git state, so there is no operational need for Claude to
  commit anything, ever, under normal workflow operation.

**Enforcement**: this is primarily a **contractual/policy boundary**
stated in the prompt/instructions under which `run-claude` invokes Claude
(the same enforcement mechanism the constitution already relies on for
"Claude stops and reports rather than deciding an architectural question
unilaterally," Principle I) — the runner does not claim to sandbox Claude
Code's own tool access at the OS level. Two things reduce the practical
risk of a violation to near zero regardless: (1) **SHOULD, if the
installed `claude` CLI supports it**: `run-claude`'s invocation SHOULD
apply the tightest tool-permission profile the CLI exposes (e.g. excluding
`git` from an allowed-commands list for its Bash tool), verified at
implementation time against the installed CLI's actual capability — the
same "verify against the real CLI, fail closed if unsupported" posture as
research.md §9, not a new one; and (2) even absent that, the existing
Candidate Tree/Fingerprint mechanism (§12, §13) still **detects** rather
than silently accepts a stray Git mutation — an unexpected commit between
a passing gate and checkpoint changes `head_oid` and is caught by the
checkpoint-time fingerprint recheck exactly like any other unauthorized
change (`CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE`); an
unexpected `git add` left uncommitted is caught by the staging-area
precondition (§0.8). Nothing in this design silently tolerates a
violation — it is prevented by policy and backstopped by detection, not
guaranteed by a technical sandbox that does not exist in v1.

### 0.12 Codex — absolute Git prohibition, and Runner-supplied evidence

**Decision**: Codex may read the candidate implementation state the Runner
exposes to it (the working directory, which — by construction — already
holds exactly the Candidate Tree's content, §12), inspect project files,
run Runner-approved read-only/non-Git validation commands, evaluate
test/build/lint results, and produce structured findings and a PASS/FAIL
bound to the Fingerprint the Runner supplied (`contracts/
codex-gate-result-contract.md`). Codex **MUST NOT** execute any Git
command, without exception — the same named list as §0.11 applies
(`status`, `diff`, `add`, `commit`, `checkout`, `switch`, `branch`,
`merge`, `rebase`, `reset`, `stash`, `tag`, `push`, branch deletion) plus,
specifically for Codex's role: it never stages, commits, merges, or tags
regardless of how confident its own PASS verdict is.

**Runner-supplied evidence**: if a review benefits from knowing "what
changed" (a diff, not just a snapshot), the **Runner** — not Codex —
generates that evidence via its own `git` access (e.g. a text diff of the
Candidate Tree against `HEAD`, produced the same way `git diff HEAD`
would render it, or simply enumerating which paths the Candidate Tree
construction in §12 touched) and includes it directly in the context/
prompt given to Codex. Codex consumes this as ordinary text; it never
requests or executes a `git` command to produce it itself.

**Enforcement**: identical posture to §0.11 — a contractual/policy
boundary in Codex's invocation, SHOULD-scoped by the installed `codex`
CLI's own tool-permission surface where available, backstopped by the
existing before/after Candidate Tree comparison around every gate
(research.md §10) which already treats *any* tree change during a Codex
session — Git-command-caused or otherwise — as `Codex gate modified the
working tree`, a hard failure regardless of Codex's own reported result.

---

## 1. Python version policy — MUST

**Decision**: Python **3.12**, pinned to an exact patch release via
`runner/.python-version` (e.g. `3.12.8`, exact patch confirmed at
implementation time against the latest 3.12.x). `pyproject.toml` declares
`requires-python = ">=3.12,<3.13"` — v1 supports exactly one minor version,
not a range, to keep the reproducibility story simple.

**Rationale**: 3.12 is mature and stable, has first-class `uv` support (`uv
python install 3.12` fetches a prebuilt interpreter on all three target
OSes without relying on any OS package manager), and — critically — Python
3.11+ ships `tomllib` in the standard library, which is what makes the
zero-dependency config story in §3 possible. Since `uv` provisions its own
interpreter, the OS is never asked to already have a suitable Python (spec
requirement, FR-038); the version choice is about stability and stdlib
surface, not OS availability.

**Alternatives considered**:
- **3.13** — newer, but no material benefit for this tool's needs (no use
  of the newer free-threading or JIT work) and a shorter track record at
  planning time; not worth being first-mover for a workflow-integrity tool.
- **3.11** — also has `tomllib`, but 3.12 is the more current stable choice
  with no compatibility cost, since `uv` provisions the interpreter either
  way.
- **A version range (e.g. `>=3.11`)** — rejected for v1: supporting a range
  adds a testing matrix dimension (which minor version's stdlib behavior is
  authoritative?) for no current benefit; a single pinned minor version is
  simpler and still fully reproducible. Widening the range is a cheap
  future change if ever needed.

---

## 2. Python package/project structure — MUST

**Decision**: Conventional `src/` layout at `runner/src/solari_workflow/`,
with `pyproject.toml` at `runner/`. See plan.md's Project Structure section
for the full module tree. CLI entry point registered via
`[project.scripts] solari-workflow = "solari_workflow.cli:main"`.

**Rationale**: `src/` layout prevents accidentally importing the
in-development package via the current working directory during tests
(a well-known packaging footgun), and is what `uv init --package` scaffolds
by default. Splitting `config/`, `speckit/`, `git/` (including
`git/candidate_tree.py`, §12), `actors/`, `readiness/`, `state/`
(block-state and the retry/resume vocabulary, §0.7), `runs/`, `lock/`, and
`platform/` into separate packages mirrors the constitution's own
principle boundaries (Reproducible Environments → `readiness/`; Auditable
Checkpoints → `git/checkpoint.py` + `git/candidate_tree.py`; Sequential
Execution → `lock/`) so each can be tested and reasoned about
independently, matching the rationale already recorded in
`docs/architecture/repository-structure.md`.

**Alternatives considered**:
- **Flat layout** (`runner/solari_workflow/*.py` at top level, no `src/`) —
  rejected: no `src/` isolation, more prone to accidental-import bugs
  during test runs.
- **One monolithic `runner.py`** — rejected: the module boundaries above
  each map to a distinct, independently testable concern (see research.md
  §18); a single file would make the fake-CLI/temp-repo test strategy
  harder to target.

---

## 3. Dependency strategy — MUST

**Decision**: **Zero third-party runtime dependencies.** `pytest` is the
only dependency, and it is a **dev-only** dependency group
(`[dependency-groups] dev = ["pytest"]` or `[project.optional-dependencies]
dev`), never installed for an end user running `solari-workflow` via `uv
tool install`.

Per-candidate evaluation (constitution Principle IX requires this
justification):

| Candidate | Needed? | Why stdlib suffices |
|---|---|---|
| YAML parser (e.g. PyYAML) | **No** | Config format is TOML (§4), not YAML — Python 3.11+ ships `tomllib` as a *read-only* TOML parser in the standard library, and the runner only ever *reads* `.ai-workflow.toml` (it is generated/owned by the orchestrator as text, never written by the runner). A read-only stdlib parser fully covers the runner's need. |
| Schema/config validator (e.g. pydantic, jsonschema) | **No** | The config schema (data-model.md) is a shallow, well-known set of tables/fields, not deep polymorphic data. Hand-written validator functions per table, returning an aggregated list of `(field, problem)` errors, give equally actionable messages with no dependency, and avoid pydantic's extra runtime/import cost for a tool whose own startup should be near-instant. |
| File locking library (e.g. `filelock`) | **No** | Single-run ownership only needs one atomic "create if absent" primitive. `os.open(path, os.O_CREAT \| os.O_EXCL \| os.O_WRONLY)` is atomic on POSIX **and** Windows via Python's own `os` module — no platform-specific third-party shim required (§7). |
| CLI framework (e.g. click, typer) | **No** | The CLI surface is a handful of subcommands with simple flag/positional arguments (contracts/cli-interface.md). Stdlib `argparse` with subparsers covers this without adding an import-time dependency to every invocation. |
| YAML/TOML *writer* | **No** | The runner never writes `.ai-workflow.toml` — it is authored/edited as text by the orchestrator (FR-015: "generated and owned by ChatGPT"). No writer capability is needed in the runner itself. |

**Rationale**: This directly satisfies Principle IX's requirement that
"every dependency... must be justified" by making the justification trivial
— there are none to justify at runtime. It also removes an entire class of
supply-chain and cross-platform-wheel-availability risk from a tool whose
job is to *guarantee* reproducibility.

**Alternatives considered**:
- **PyYAML + jsonschema** (the spec's original placeholder direction) —
  rejected once `tomllib` was identified as the better fit: it would add
  two dependencies to do what stdlib does for one format switch, with no
  behavioral requirement that specifically needs YAML over TOML (both are
  "human-readable" per FR-015; TOML's stdlib-read support is the
  deciding factor, not a stylistic preference).
- **`filelock` third-party package** — rejected: correct and popular, but
  `os.open(..., O_EXCL)` is already atomic cross-platform for this
  narrower use case (a single advisory ownership marker, not general
  file-region locking), so the dependency buys nothing.
- **`click`/`typer`** — rejected for v1: nicer ergonomics, but `argparse`
  fully covers a fixed, small subcommand set; revisit only if the CLI
  surface grows substantially (SHOULD-if-ever, not v1).
- **pytest as a runtime dependency** — never considered; it is correctly
  scoped as dev-only and does not affect the shipped tool's footprint.

---

## 4. Project configuration format — MUST

**Decision**: **TOML**, canonical filename **`.ai-workflow.toml`**, at the
consuming project's root. Parsed exclusively with stdlib `tomllib`.

This supersedes the spec's stated "current architectural preference"
(`.ai-workflow.yml`) — the spec's own clarification log explicitly reserves
this choice for planning: *"format decided during planning... unless
planning identifies a better alternative... no format is locked in this
specification."* `tomllib` being stdlib-only for a read-only consumer is
exactly such a better alternative, so this is a deliberate, documented
supersession, not a deviation from spec intent.

**Rationale**:
- TOML supports comments, typed values, and nested tables — everything YAML
  offers for this use case — while `tomllib` ships in Python 3.11+'s
  standard library for *reading* (§3). YAML has no standard-library reader.
- TOML's table syntax (`[environment]`, `[environment.docker]`) maps
  cleanly onto the config's naturally sectioned shape (data-model.md),
  and optional tables that are simply absent (no Docker, no local model)
  read as "key not present" rather than needing an explicit `null`/`~` —
  a good fit for FR-021's "skip, don't fail" readiness semantics.
- TOML has no YAML-style ambiguous scalar parsing (the classic
  `country: NO` → boolean footgun) — one less class of misconfiguration.

**Alternatives considered**:
- **YAML** — rejected: requires a third-party parser (§3) for a
  read-only stdlib-only goal; more permissive/ambiguous scalar grammar.
- **JSON** — rejected: stdlib-supported, but no comments, and worse
  hand-editing ergonomics for a config a human/ChatGPT edits directly;
  offers no advantage over TOML here.
- **Structured Markdown front matter** — rejected: would need a bespoke
  parser (no stdlib support for the specific structured shape needed,
  unlike TOML), for no readability gain over TOML for this schema shape.

---

## 5. Configuration validation — MUST

**Decision**: Fail-fast, two-tier validation, always reporting **every**
problem found (not just the first):

1. **Parse errors** (`tomllib.TOMLDecodeError`) surface the file path and
   the parser's own line/column message verbatim.
2. **Schema errors**: after parsing, `config/schema.py` validators walk the
   parsed dict against the expected shape (data-model.md) and collect a
   list of `(path, problem)` pairs:
   - `[workflow].schema_version` **MUST** be present and equal to a version
     v1 recognizes (only `"1"` for v1). An absent or unrecognized value is
     a hard failure naming the supported version(s).
   - Required fields per table (data-model.md marks each field
     required/optional/stack-conditional) are checked for presence and
     type. Stack-conditional sections (`[environment.docker]`,
     `[environment.local_model]`) are validated *if present*; their
     absence is never an error by itself.
   - **Unknown-field handling**: an unrecognized key or table anywhere is a
     **warning** printed to stderr, not a hard failure. This keeps the
     config forward-compatible (an orchestrator using a newer workflow
     schema version's optional field against an older runner degrades
     gracefully) while missing/malformed *required* fields still fail
     fast. This is the simplest rule that satisfies "fail-fast on actual
     problems" without penalizing additive, backward-compatible schema
     growth.
3. All collected schema errors are reported together in one CLI failure,
   each naming the exact TOML path (e.g. `environment.lockfile`) and the
   problem, so a user fixes everything in one pass rather than
   whack-a-mole.

**Backward compatibility rule for v1**: only `schema_version = "1"` is
accepted; there is nothing to be backward-compatible *with* yet. A future
`"2"` will define its own migration note per the constitution's Governance
section — no implicit coercion between versions is ever performed.

**Alternatives considered**:
- **Stop at first error** — rejected: worse UX for a human/ChatGPT fixing
  a freshly generated config; aggregating costs nothing extra.
- **Unknown fields are always errors** — rejected: would make every future
  additive schema change a breaking change for older runners, which
  conflicts with the constitution's amendment/migration model (additive
  fields shouldn't require every consumer to upgrade in lockstep).
- **Unknown fields are always silently ignored (no warning)** — rejected:
  silent ignoring hides real typos (e.g. `lockfle` instead of `lockfile`
  would silently fall back to "absent" and could pass a readiness check it
  shouldn't); a warning gives visibility without hard-failing.

---

## 6. Cross-platform subprocess execution — MUST

**Decision**: One thin wrapper, `platform/proc.py`, used by every module
that shells out (`git`, `claude`, `codex`, `uv`, and configured project
validation commands):

- Always `subprocess.run`/`subprocess.Popen` with **list-form argv**,
  **never** `shell=True` and never manual shell-string concatenation.
- Executable resolution via `shutil.which(...)` up front, producing an
  explicit "`<tool>` not found on PATH" error rather than deferring to
  whatever the OS shell would have resolved — keeps the error message
  identical across macOS/Windows/Linux.
- `run(argv, cwd, env=None, check=False, capture=True) -> ProcessResult
  (returncode, stdout, stderr, duration)`, decoded as UTF-8 text with
  `errors="replace"` so odd bytes from a subprocess never crash the
  runner.
- Non-zero exit codes are **data**, not automatically exceptions: readiness
  checks and configured build/test/lint commands explicitly inspect
  `returncode` to decide PASS/FAIL. A separate `CommandFailed` exception
  exists for cases where a non-zero exit is unambiguously a runner-internal
  error (e.g. `git` itself failing during a checkpoint step), caught at the
  CLI boundary and turned into an actionable message — exit-code semantics
  are decided per call site, not globally.
- **Streaming + persistence for `claude`/`codex`**: long-running sessions
  are run via `Popen` with stdout/stderr read line-by-line on background
  threads (stdlib `threading`), simultaneously (a) echoed to the operator's
  terminal and (b) written verbatim into the run's `.ai-runs/*-result.md`
  transcript — no third-party process-streaming library needed.
- **Config-provided commands** (test/build/lint) are declared in
  `.ai-workflow.toml` as an **array of argv tokens** (preferred,
  unambiguous cross-platform) or, if given as a single string, tokenized
  with `shlex.split()` for convenience — but still executed as list-argv
  with no shell, so shell metacharacters in a config value are inert data,
  not commands. Because of this, v1 does not support shell pipes/redirects
  directly in a configured command; a project needing shell features wraps
  them in its own script (`scripts/test.sh` / `scripts/test.ps1`) and
  configures that script as the command — this keeps command-injection
  surface at zero regardless of config content.

**Rationale**: List-argv + no shell is the standard defense against
injection from config-sourced strings, and is naturally cross-platform
since Python's `subprocess` module normalizes argv handling on Windows
(`CreateProcess` argument quoting) versus POSIX `execve` internally.

**Alternatives considered**:
- **`shell=True` with careful quoting** — rejected outright: quoting rules
  differ between POSIX shells and `cmd.exe`/PowerShell, and any config
  value or Spec Kit task title reaching a shell string becomes an
  injection vector.
- **A process-supervision library (e.g. `sh`, `pexpect`, `plumbum`)** —
  rejected: stdlib `subprocess` + `threading` fully covers streaming and
  capture for this tool's needs (no interactive TTY/PTY requirement),
  so a dependency buys nothing (§3).

---

## 7. Single-run ownership / locking — MUST

**Decision**: An atomic, `O_EXCL`-created lock file at
**`.ai-runs/.lock`** (inside the directory already required to exist and be
Git-ignored — no new ignored path to add).

- **Acquire**: `os.open(".ai-runs/.lock", os.O_CREAT | os.O_EXCL |
  os.O_WRONLY)`. This is atomic on POSIX and on Windows via Python's `os`
  module — no platform-specific code path needed, and no third-party
  `filelock` dependency (§3). On success, the runner writes a JSON payload:
  `pid`, `hostname`, `tool`, `model`, `effort`, `session_mode`, `scope`,
  `purpose`, `started_at` (UTC ISO-8601), `lock_schema_version`.
- **Refuse on contention**: `FileExistsError` → read the existing payload
  and fail with an actionable message naming the owning tool, PID,
  hostname, purpose, and start time, plus the manual recovery instruction
  below. The attempt is refused, never queued or silently retried (matches
  User Story 3's "refused... until the first execution ends").
- **Stale-ownership detection (best-effort, non-destructive)**: if the
  lock's `hostname` matches the current machine, `platform/proc.py`
  attempts a liveness check (`os.kill(pid, 0)` on POSIX; a Windows-specific
  equivalent at implementation time) and annotates the refusal message with
  "process appears to still be running" / "process does not appear to be
  running — if you have confirmed no workflow process is active, remove
  `.ai-runs/.lock` manually" / "liveness could not be determined." The
  runner **never** auto-removes a lock file itself — recovery from a truly
  stale lock is always an explicit, manual, human action, per the spec's
  "no destructive automatic recovery" requirement.
- **Release**: the lock is removed in a `finally` block wrapping the full
  owned execution (one readiness check, one `claude`/`codex` invocation, or
  one checkpoint operation — each CLI subcommand invocation owns the lock
  for its own duration only).

**Relationship to block-state (§0.7)**: the run lock and
`.ai-runs/.block-state.json` are deliberately separate, differently-scoped
pieces of state. The lock exists only while *one process* is actively
running (acquired and released within a single CLI invocation); block-state
persists *across* invocations for the lifetime of one block (from
`start-block` through `checkpoint`) so a `resume` after a crash knows where
to pick up. Neither subsumes the other: a resume after a crash finds no
lock held (the crashed process's lock was never released cleanly — see the
stale-detection note above, which still applies) but finds block-state
recording exactly which stage last completed safely.

**Rationale**: Satisfies FR-005/FR-006 as a *technical* guarantee (the
second process's `os.open` call itself fails, not merely a documented
convention), with zero third-party dependencies and identical behavior on
all three target OSes.

**Alternatives considered**:
- **`filelock` package** — rejected per §3: no capability gap it fills
  for this single-owner-marker use case.
- **PID file without `O_EXCL`** (check-then-write) — rejected: a
  check-then-write is a classic TOCTOU race between two runner invocations
  starting at nearly the same time; `O_EXCL` create is atomic and closes
  that race entirely.
- **OS-native advisory locks (`fcntl.flock` / `msvcrt.locking`)** —
  rejected: would need two separate platform code paths and behaves
  differently on network filesystems; a single `O_EXCL` file works
  identically everywhere `os.open` does.
- **Automatic stale-lock removal after a timeout** — rejected: violates
  the explicit "no destructive automatic recovery" requirement; a workflow
  tool whose entire purpose is auditable integrity must never silently
  assume another process is dead.

---

## 8. `.ai-runs/` audit model — MUST

**Decision**: Append-only, immutable prompt/result pairs.

- **Sequence numbering**: for the current UTC date, list existing
  `.ai-runs/YYYYMMDD-*-*-prompt.md` files, take the max `NNN` used that day,
  and use `max+1` (zero-padded to 3 digits). Because the run lock (§7) is
  held for the full duration of the owning execution, there is no
  cross-process race to resolve here beyond the numbering scan itself.
- **Atomic creation as a final safety net**: the prompt file is still
  created with `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)`; on an
  unexpected collision (e.g. a manually placed file with the same name),
  the runner retries with `NNN+1` (bounded to a small number of retries)
  rather than ever overwriting — directly satisfies FR-024/SC-007.
- **Slug**: derived from the block/task/purpose text using the same
  ASCII-normalization pipeline as PascalCase derivation (§16), lower-cased
  and hyphen-joined, so both derivations share one tested normalization
  routine.
- **Prompt file metadata** (plain Markdown headers — not another
  machine-parsed config, since nothing needs to re-parse this file
  programmatically in v1): Tool, Model, Effort/Reasoning, Session Mode
  (`NEW` or `CONTINUE` + prior session id when `CONTINUE`), Spec Kit
  involvement, Scope (task IDs/block name), Purpose, Timestamp (UTC),
  workflow schema/version. Never a raw environment-variable *value* — only
  variable *names*, if referenced at all.
- **Result file metadata**: the same header block plus the outcome (e.g.
  gate PASS/FAIL, checkpoint VERIFIED, or an implementation summary) and an
  explicit pointer back to its paired prompt file by shared
  `YYYYMMDD-NNN-slug` stem.
- **Immutability**: the runner never re-opens an existing prompt/result
  file for editing. Every execution or gate — including a re-gate after
  remediation — creates a brand-new numbered pair.

**Rationale**: Directly implements FR-022–FR-024 and SC-007 with the same
`O_EXCL` primitive already justified in §7, keeping the dependency count at
zero.

**Alternatives considered**:
- **A single append-only log file instead of per-run files** — rejected:
  the spec's own required naming pattern is per-run file pairs
  (`YYYYMMDD-NNN-slug-{prompt,result}.md`), and per-file immutability is
  simpler to guarantee (`O_EXCL` per new file) than safe concurrent
  appends to one growing file.
- **A database (e.g. SQLite) for run history** — rejected: adds a stateful
  store to what the spec defines as plain files, for no benefit at this
  scale (§3 Scale/Scope).

---

## 9. Claude Code integration — MUST

**Decision**: The runner invokes the `claude` CLI (located via
`shutil.which("claude")`, §6) through `actors/claude.py`, passing model,
effort, and session-mode as **explicit parameters supplied by the
orchestrator on every invocation** — the runner itself never defaults to or
infers `CONTINUE`. A new coherent block defaults to `NEW` at the
orchestrator-decision level (FR-008); `actors/claude.py` simply refuses to
run without an explicit mode argument, so "default to NEW" is enforced by
requiring the orchestrator to say so, not by the runner silently assuming
it.

The exact CLI flags used to select model/effort and to start a session in
`NEW` vs. `CONTINUE`/`RESUME` mode **MUST be verified against the installed
`claude` CLI's own `--help`/documentation at implementation time**, since
CLI flags evolve independently of this plan. `actors/claude.py` is
structured so that flag mapping lives in one small, isolated function
(`_build_claude_argv(...)`), and the runner **fails closed** — with an
explicit, actionable error — if the installed CLI does not support a
requested capability (e.g. an effort flag that doesn't exist in the
installed version), rather than silently dropping the request or falling
back to an unrequested mode.

Every `claude` invocation is wrapped by the run lock (§7) and produces a
`.ai-runs/` prompt/result pair (§8) recording the tool/model/effort/session
mode actually used, regardless of exactly which flags produced it.

**Claude status contract (new, supports §0.6/§0.7)**: the prompt given to
Claude requires its final output to end with a small, parseable marker —
mirroring Codex's structured contract (§10) but simpler, since it is a
status, not a verdict-with-findings:

```
STATUS: COMPLETE|ARCHITECTURAL_DECISION_REQUIRED|BLOCKED
```

- `COMPLETE` — implementation and self-validation finished; safe to
  proceed to `run-codex-gate`.
- `ARCHITECTURAL_DECISION_REQUIRED` — Claude determined an out-of-scope
  architectural decision is needed (constitution Principle I/FR-003); the
  runner records block-state `ARCHITECTURAL_DECISION_REQUIRED` (§0.7) and
  stops. It is never auto-retried (§0.6).
- `BLOCKED` — Claude could not complete for a reason it can describe but
  that isn't an architectural decision (e.g. a missing prerequisite it
  cannot resolve within scope); recorded as `BLOCKED_MANUAL`.
- **Missing or unparseable marker** → treated as `BLOCKED` (fail-closed,
  same philosophy as the Codex result contract in §10 — the runner never
  assumes `COMPLETE` from an ambiguous or absent status).

This marker is parsed by `run-claude` purely to update block-state (§0.7)
and choose an exit code; it never causes the runner to itself change any
code — that stays entirely Claude's own work within the invocation. Claude
never touches Git at all during that work (§0.11) — the branch already
exists, checked out by `start-block` before this invocation begins, and
Claude's edits simply land on disk for the Runner's own Candidate Tree
construction (§12) to pick up afterward.

**Rationale**: Preserves FR-007/FR-008/FR-009 and Principle I (session mode
is the orchestrator's decision, never the runner's) while avoiding baking
in a specific CLI flag surface that this plan cannot verify is current;
isolating flag-construction into one function keeps that verification
step small and localized for the implementation phase.

**Alternatives considered**:
- **Hard-coding specific flags now** — rejected: the planning brief
  explicitly warns not to assume CLI details unsupported by the currently
  available Claude CLI; isolating and deferring exact flags to a
  verified implementation step is the safer, still-concrete choice.
- **Always start a fresh session and never support CONTINUE** — rejected:
  contradicts FR-009's explicit requirement for an orchestrator-chosen
  CONTINUE mode for immediate remediation.

---

## 10. Codex integration — MUST

**Decision**: `actors/codex.py` always starts a **NEW** Codex session for
every gate or re-gate — there is no CONTINUE/RESUME concept for Codex at
all (FR-008, unconditionally). Two independent, technical backstops enforce
the read-only gate contract beyond the CLI's own behavior:

1. **Candidate Tree comparison around the gate**: the runner builds the
   Candidate Tree and its Fingerprint X (§12) immediately before invoking
   `codex`, and rebuilds the Candidate Tree again immediately after it
   exits; if the `candidate_tree_oid` differs, the gate is a hard failure —
   `Codex gate modified the working tree` — regardless of what Codex itself
   reported. This makes the read-only contract technically checked, not
   merely requested in the prompt.
2. **Structured result contract**: the prompt given to Codex requires it to
   end its output with an unfenced, column-0 marker block the runner can parse
   deterministically (see `contracts/codex-gate-result-contract.md`):
   ```
   RESULT: PASS|FAIL
   FINDINGS:
   - severity: blocker|major|minor|info
     summary: ...
   ```
   If this marker is missing or unparseable, the gate outcome is **FAIL**
   — the runner never defaults an ambiguous or malformed result to PASS.
   This is the concrete mechanism behind "Codex must not waive its own
   failures": the runner's parser decides PASS/FAIL from the structured
   severity list, and `[checkpoint].blocking_severities` in
   `.ai-workflow.toml` (default `["blocker", "major"]`) — not Codex's own
   prose summary — decides whether any given PASS is checkpoint-eligible.

Every Codex gate is wrapped by the run lock (§7) and produces its own
`.ai-runs/` prompt/result pair (§8), including Fingerprint X computed at
gate time (§12) for later checkpoint verification. Codex itself never
touches Git (§0.12) — any repository context it needs (e.g. a diff view)
is prepared and handed to it by the Runner as plain text, not obtained by
Codex running `git` itself.

**Rationale**: Directly satisfies FR-004 (read-only), FR-008 (always NEW),
and the constitution's "the same actor never both fixes and re-approves...
in the same gate" — with a technical check (Candidate Tree comparison)
rather than relying solely on trusting Codex's own report.

**Alternatives considered**:
- **Trusting Codex's stated PASS/FAIL without a structured contract** —
  rejected: gives Codex the practical ability to "waive its own failures"
  through prose framing; a parseable, severity-classified marker with a
  fail-closed default removes that ambiguity.
- **Relying only on a CLI-level read-only/sandbox flag, if one exists** —
  kept as a SHOULD-if-available layer on top of, not instead of, the
  Candidate Tree comparison — a flag alone cannot be verified
  deterministically by the runner itself the way a before/after tree-OID
  comparison can.

---

## 11. Project Readiness Gate architecture — MUST

**Decision**: **One core readiness engine, with stack-specific checks
registered in a small internal dict — not a discoverable/dynamic plugin
system.**

- `readiness/engine.py` runs a fixed list of **common checks** (config
  valid, required Spec Kit artifacts present, `.ai-runs/` Git-ignored, main
  branch identified, checkpoint strategy defined, no obvious secrets in
  config via a heuristic scan) unconditionally, plus a **stack-specific**
  check list selected by `[environment].stack` from a literal registry:
  `{"python": python_stack.CHECKS, "node": node_stack.CHECKS, "go":
  go_stack.CHECKS, "rust": rust_stack.CHECKS}`. Adding a new stack means
  adding a module and one registry entry — no dynamic loading, no external
  plugin packages, matching the instruction to avoid overengineering a
  generic plugin system for v1.
- Additional **conditional sections** apply regardless of stack, purely
  based on whether their config table is present: `[environment.docker]`
  present → Docker tag-pinning checks run; `[environment.local_model]`
  present → local-model/Ollama version+digest checks run. Absent → the
  section is reported `SKIPPED`, never `FAIL` (FR-021, SC-006).
- Every check returns `CheckResult(name, status: PASS|FAIL|SKIPPED,
  message, severity)`. Gate-level (overall) status is one of
  `FAIL|INCOMPLETE|PASS`, precedence `FAIL > INCOMPLETE > PASS`: `FAIL` if
  any executed `CheckResult` is `FAIL`; else `INCOMPLETE` if a required
  stack-specific readiness implementation is missing or empty (e.g. a
  registered stack whose `CHECKS` list a required section expects is
  absent/empty); else `PASS` (all required common and registered
  stack-specific checks exist and pass — `[environment].stack = "other"`
  has no stack-specific implementation to be missing, so it uses common
  checks alone and produces a normal `PASS`/`FAIL`). The full list
  (including `SKIPPED` entries) is always reported for transparency, so a
  user sees *why* a section was skipped, not just that it was.

**Rationale**: A registry-dispatch engine gives exactly the "conditional on
actual stack" behavior User Story 4 and FR-020/FR-021 require, at the
smallest architecture that supports it — each stack's checks are an
isolated, independently testable module (§18), without the indirection
cost of a discoverable plugin system nobody has asked for in v1.

**Alternatives considered**:
- **A dynamic plugin-discovery system** (entry points / drop-in directory
  scanning) — rejected for v1: no current requirement for third parties to
  add stacks without touching this repository; the registry dict is
  trivially extensible by a code change and far simpler to test and reason
  about.
- **One monolithic check function per project** — rejected: would mix
  common and stack-specific concerns in one function, making "skip vs
  fail" harder to keep consistent and each check harder to unit-test in
  isolation.

---

## 12. Validated-state fingerprint — MUST (corrected: Candidate Tree design)

**This section replaces the prior draft's assumption that Codex reviews an
already-committed, clean tree.** That assumption was wrong: per §0.9, the
implementation state must remain **uncheckpointed** until QA approval —
Codex reviews the exact uncommitted state a block is proposing.

**Decision**: build an isolated **Candidate Tree** using a temporary Git
index (the real `.git/index` is never read or written during this step),
and bind the fingerprint to that tree's own content-addressed OID rather
than to a hand-rolled hash of a diff.

### Candidate Tree construction

```
tmp_index = <repo>/.git/solari-workflow/tmp-index-<random>   # never inside the working tree

GIT_INDEX_FILE=$tmp_index git read-tree HEAD     # seed temp index from HEAD's tree
GIT_INDEX_FILE=$tmp_index git add -A             # stage the CURRENT WORKING DIRECTORY
                                                  # into the temp index only
candidate_tree_oid = $(GIT_INDEX_FILE=$tmp_index git write-tree)
rm -f $tmp_index                                 # cleanup, always in a `finally` block
```

- `git add -A` against a redirected `GIT_INDEX_FILE` reads the working
  directory's current on-disk content and stages it into the *temporary*
  index — it never touches `.git/index`. This is the standard, safe
  technique for building a hypothetical tree without disturbing the real
  staging area (the same primitive `git stash` itself relies on
  internally).
- `git add -A` is `.gitignore`-aware by construction, so `.ai-runs/` (and
  the lock/block-state files inside it) are automatically excluded —
  **this is exactly why the readiness gate's "`.ai-runs/` excluded from
  Git" check (§11) is load-bearing for correctness, not just tidiness**:
  if that check were ever wrong, operational files could leak into the
  Candidate Tree.
- Tracked modifications, deletions, and new non-ignored files are all
  captured by `git add -A` in one pass; **file modes and symlinks are
  handled natively** by Git's own `lstat`-based staging — no custom mode
  or symlink logic is needed in the runner.
- This construction needs **no precondition on the working directory
  itself** — uncommitted, uncomitted-and-unstaged, and untracked content
  are all exactly what's expected to exist there (that *is* the proposed
  implementation). The real staging area, by contrast, has its own
  precondition (§0.8) because the runner will later write to it (§13).

### Fingerprint

A fingerprint is a **structured record**, not a single opaque hash — the
Git object OIDs it's built from are already collision-resistant, so
wrapping them in a further hash adds nothing to the authoritative
equality check:

```
Fingerprint X = {
  head_oid: <git rev-parse HEAD>,
  candidate_tree_oid: <as computed above>,
  branch_name: <current branch>,
  first_task, last_task: <block's task range, for audit binding>,
  computed_at: <UTC timestamp>
}
```

A short derived id (`sha256(f"{head_oid}:{candidate_tree_oid}")[:16]`) may
be used purely as a compact label in filenames/logs — it is never used for
the authoritative equality check, which always compares `head_oid` and
`candidate_tree_oid` directly.

### Before/after-Codex check (technical read-only backstop, unchanged in spirit from the original draft)

`run-codex-gate` computes Fingerprint X **before** invoking `codex`
(this is what Codex's session is authorized against) and recomputes the
Candidate Tree **immediately after** `codex` exits. If `candidate_tree_oid`
differs between the two — regardless of `head_oid`, which shouldn't move
either — the gate is a hard failure: `Codex gate modified the working
tree`, independent of whatever `RESULT:` Codex itself reported
(`contracts/codex-gate-result-contract.md`).

### Where stored

Fingerprint X, once Codex's structured result parses as `PASS` with no
before/after mismatch, is written into that gate's immutable
`.ai-runs/*-result.md` (the authoritative, permanent record) and mirrored
into `.ai-runs/.block-state.json`'s `last_gate` field (§0.7) as a fast,
disposable index.

### Recheck at checkpoint time

Immediately before `checkpoint` stages anything (§13), the runner:
1. confirms the current branch name matches Fingerprint X's `branch_name`;
2. confirms `git rev-parse HEAD` still equals Fingerprint X's `head_oid`;
3. rebuilds the Candidate Tree fresh (same construction as above) and
   confirms its OID equals Fingerprint X's `candidate_tree_oid`.

**What invalidates it**: any difference in `head_oid` or
`candidate_tree_oid` — a new commit, or any change to a tracked or
untracked-and-not-ignored file since the gate. No fuzzy or partial
matching.

**Required behavior on mismatch** (exact, unchanged from the original
requirement):

```
CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE
```

The block must be re-gated (a new Codex session, producing a new
Fingerprint X) before a checkpoint can be attempted again.

**Alternatives considered**:
- **The original draft's `sha256(git diff HEAD --binary + untracked file
  hashes)` design** — superseded: it implicitly assumed gating only ever
  happens against a clean, already-committed tree (so the diff would
  normally be empty), which contradicts the corrected requirement that
  Codex reviews **uncommitted** work. A content-addressed Candidate Tree
  built from a temporary index is the right primitive regardless of how
  much uncommitted content exists.
- **Fingerprint = `git diff HEAD` alone** — still rejected, for the same
  reason as before: it does not capture untracked files, and a diff is not
  a content address the runner can later feed directly to `git read-tree`/
  `git commit-tree` (§13) the way a tree OID can.
- **Hashing the Candidate Tree OID together with other fields into one
  opaque SHA-256** — rejected as unnecessary: `candidate_tree_oid` and
  `head_oid` are themselves exactly the values that must match; hashing
  them together would only make debugging a mismatch harder (you'd lose
  which of the two changed) for no security benefit.

---

## 13. Git checkpoint flow — MUST (corrected: staging precondition + Candidate Tree invariant chain)

**Decision**: exact sequence, run only via the dedicated `checkpoint`
subcommand. This corrects the prior draft's step 5 ("no separate
checkpoint commit needed because the tree is already committed") — the
runner now performs the actual checkpoint commit itself, from the
Candidate Tree, per §0.1's ownership model.

1. **Branch verification** — current branch matches the expected block
   branch pattern `T{first_task}-{PascalCaseBlockName}` (§16) and matches
   the specific branch `checkpoint` was invoked for; refuse if on `main`
   or an unrelated branch.
2. **Real staging-area precondition** (§0.8) — `git diff --cached --quiet`
   must succeed (real index matches `HEAD`); refuse with an actionable
   message (naming the unexpectedly staged paths) otherwise, without
   touching the index.
3. **Gate PASS verification** — the latest gate record for this branch
   (via `.ai-runs/.block-state.json`'s `last_gate`, falling back to
   scanning `.ai-runs/` result files if the cache is missing or stale)
   reports `PASS` with zero unresolved findings at or above
   `[checkpoint].blocking_severities`.
4. **Fingerprint recheck** (§12) — rebuild the Candidate Tree fresh and
   require exact equality with the recorded Fingerprint X's `head_oid` and
   `candidate_tree_oid`; mismatch → `CHECKPOINT BLOCKED — WORKING TREE
   CHANGED AFTER GATE`, stop.
5. **Stage the approved state into the *real* index**: now that Candidate
   Tree X is proven current and precondition (2) proved the real index was
   safe to overwrite, run `git read-tree <candidate_tree_oid>` directly
   against the real `.git/index` (no `GIT_INDEX_FILE` redirect this time —
   this is the one intentional, now-safe write to the real staging area in
   the whole flow) **and** update the working directory to match via
   `git checkout-index -a -f` if it does not already (in the common case
   the working directory already *is* the candidate content, since that's
   what the Candidate Tree was built from — this step exists for
   correctness, not because drift is expected).
6. **Verify the staged tree**: `git write-tree` against the now-updated
   real index **MUST** equal `candidate_tree_oid` — a direct implementation
   of the required invariant chain's third link (below). Any mismatch here
   is treated as an internal runner error (not a user-facing "blocked"
   state) and stops immediately without committing.
7. **Create the checkpoint commit via plumbing**, not `git commit`:
   `git commit-tree <candidate_tree_oid> -p <head_oid> -m "<message>"`
   (message per §14) — using `commit-tree` directly guarantees the
   resulting commit's tree is *exactly* `candidate_tree_oid` by
   construction (not by re-reading a possibly-drifted working directory),
   and bypasses any local commit hooks that could otherwise interfere.
8. **Advance the branch ref**: `git update-ref refs/heads/<branch>
   <new_commit_oid> <head_oid>` — the compare-and-swap form, which itself
   fails if `HEAD`'s branch ref moved since step 4 (a second, free
   safety net on top of the fingerprint recheck).
9. **Verify the commit tree**: `git rev-parse --verify <new_commit_oid>^{tree}`
   **MUST** equal `candidate_tree_oid` — the required invariant chain's
   fourth link. This is cheap insurance given step 7 already guarantees it
   by construction.
10. **Switch to `main`** (`git switch main`) and confirm `main` itself has
    no unexpected local divergence.
11. **Merge**: `git merge --no-ff <block-branch>` — an ordinary porcelain
    merge of two already-verified, known-good branches; this step is the
    one place in the flow that legitimately updates the working
    directory/index via normal Git merge machinery, since both sides are
    already trusted.
12. **Verify the merge** — non-zero exit or conflict markers is a hard stop
    with an actionable message; the runner **never** attempts automatic
    conflict resolution.
13. **Create the annotated tag** `checkpoint-T{first}-T{last}` on the
    resulting `main` commit, with the full metadata contract from §14.
14. **Branch retention** (§15) — safe, non-force cleanup of eligible old
    merged branches only.
15. **No push** — ever, as part of this flow (§0.2).
16. **Audit record + block-state update** — a final `.ai-runs/*-result.md`
    entry records the checkpoint outcome; `.ai-runs/.block-state.json`'s
    `state` is set to `COMPLETED`. The command then exits; it never starts
    a next block automatically (FR-011, Acceptance Scenario 5; §0.3).

### Required invariant chain (explicit)

```
Codex-reviewed Candidate Tree X
  → Fingerprint X                      (§12, computed before Codex)
  → staged Git tree X                  (step 6 above: real index write-tree == candidate_tree_oid)
  → checkpoint commit tree X           (step 9 above: commit^{tree} == candidate_tree_oid)
```

Every arrow is independently verified rather than assumed — a break at any
link stops the flow with an actionable message rather than proceeding on
the belief that an earlier check makes a later one redundant.

**Rationale**: Each step is independently verifiable and stops hard on any
unexpected state rather than guessing forward — matching Principle VII
(Controlled Automation) and Principle VI (Auditable Checkpoints). Using
`commit-tree`/`update-ref` plumbing instead of `git commit` removes any
dependency on the working directory or real index being in a particular
shape at commit time beyond what step 5 already established.

**Alternatives considered**:
- **The original draft's "no separate checkpoint commit — the branch tip
  already is the checkpoint"** — superseded: it assumed implementation was
  already committed by Claude before gating, which contradicts §0.9's
  correction that the state stays uncommitted until QA approval. The
  runner now performs exactly one commit, authored from the proven
  Candidate Tree.
- **Using `git commit` instead of `commit-tree`/`update-ref`** — rejected:
  `git commit` reads the real index and `HEAD` at the moment it runs,
  which reintroduces exactly the class of TOCTOU risk (something could
  have touched the real index between staging and committing) that
  plumbing commands avoid by taking the tree OID as an explicit,
  already-verified argument.
- **Auto-resolving trivial merge conflicts** — still rejected outright: the
  spec requires no automatic recovery of ambiguous states; a human/
  orchestrator decision is always required on conflict.

---

## 14. Annotated checkpoint tags — MUST (required fields) / SHOULD (optional fields)

**Decision**: `git tag -a checkpoint-T{first}-T{last} -m "<message>"` with
a plain-text message built from data already on hand — no extra test/model
runs to populate it (FR-033):

```
Checkpoint: {block_name}

Branch: T{first}-{PascalCaseBlockName}
Task range: T{first}-T{last}
Tasks:
  - T091: <title from tasks.md>
  - T092: <title from tasks.md>
  ...
Gate: PASS
Checkpoint: VERIFIED

Tests: <passed>/<total> passed        # only if already reported by the gate
Known deferred failures: <list|none>  # only if already recorded
Lint: <result>                        # only if already reported
Determinism: <result>                 # only if already reported
```

Task titles are read directly from Spec Kit's own `tasks.md` for the
recorded task-ID range — a read-only lookup at tag time, never a rerun of
anything. Optional fields are included **only when already present** in
the parsed Codex gate result or Claude's self-validation summary already
sitting in `.ai-runs/`; any field with no available data is omitted
entirely from the message rather than filled with a placeholder.

**Rationale**: Matches FR-032/FR-033 and the constitution verbatim —
required metadata is non-negotiable, optional metadata is strictly
best-effort and never a reason to do extra work.

**Alternatives considered**:
- **A structured tag message format (e.g. embedded JSON/YAML in the tag
  annotation)** — rejected: `git tag -l --format` and `git show` render
  plain text most naturally for a human audit trail; a human reading
  `git log --tags` benefits more from readable headers than an embedded
  machine format, and nothing in v1 needs to *parse* tag messages back
  (the authoritative structured record is the `.ai-runs/` result file).

---

## 15. Branch retention — MUST

**Decision** (formalizing the constitution's stated policy as an
algorithm):

1. Enumerate branches matching `T\d+-[A-Za-z0-9]+` (the dev-branch pattern)
   that are fully merged into `main`: `git branch --merged main`, plus a
   `git merge-base --is-ancestor <branch> main` double-check per candidate.
2. **Exclude** the currently checked-out/active branch unconditionally,
   regardless of merge state or age.
3. Sort the remaining merged candidates by their tip commit's committer
   date, descending (documented v1 approximation for "most recently
   merged" — the tip commit's own date, not a separately tracked merge
   timestamp, since Git does not retain a first-class "when was this
   branch merged" fact once the ref itself may be deleted).
4. **Keep** the top 2; every older candidate is eligible for removal.
5. **Delete** eligible candidates with `git branch -d` (safe/non-force —
   Git itself refuses if a branch turns out not to be fully merged,
   providing a second safety net beyond step 1's own check). **Never**
   `git branch -D`.
6. **Never** touch tags, and **never** touch `main`.
7. **Statelessness / "prospective only"**: the algorithm is recomputed
   fresh from current Git state and the current config every time it runs
   — it keeps no record of past retention runs to replay. FR-035's
   "historical branches that predate a retention policy MUST NOT be
   retroactively rewritten" is satisfied structurally: retention only ever
   *deletes a ref*, never rewrites commits/tags, and a change to
   `[branch_retention]`'s keep-count in config simply changes what "top 2"
   means going forward — it never reaches back to reinterpret branches
   that were already deleted under a prior run.
8. **Remote/upstream mismatch handling**: v1 retention is **local-only** —
   it never fetches, pushes, or deletes a remote-tracking branch,
   consistent with "no push" being a hard constraint throughout this
   workflow (§0.2). If a local branch has a diverged or ambiguous upstream,
   `git branch -d`'s own safe/non-force semantics apply unchanged: it
   refuses to delete rather than forcing the issue, so a
   remote/upstream mismatch **fails safe by leaving the branch intact**,
   never by escalating to `-D`. A stale remote-tracking ref left behind by
   a local branch deletion is an explicitly **DEFERRED** concern (out of
   scope until push automation itself is designed).

**Alternatives considered**:
- **Tracking a persistent "merge order" ledger** — rejected: adds a new
  piece of mutable state to keep in sync with Git reality, for a
  recency signal Git's own commit dates already approximate well enough
  for a "keep the 2 most recent" policy.
- **Force-deleting to guarantee retention counts exactly** — rejected
  outright: the spec explicitly forbids force-delete "merely to satisfy
  retention."

---

## 16. PascalCase block-name derivation — MUST

**Decision**: a deterministic, documented v1 rule (shared by branch naming
and `.ai-runs/` slugs, §8):

1. **Normalize**: `unicodedata.normalize("NFKD", name)`, then drop
   characters in Unicode category `Mn` (combining marks) — transliterates
   accented Latin text to its ASCII base (e.g. `"café"` → `"cafe"`).
2. **Word-split**: split on any run of characters outside `[A-Za-z0-9]`
   (regex `[^A-Za-z0-9]+`) — this covers spaces, hyphens, underscores, and
   punctuation uniformly as word boundaries.
3. **Casing per word**:
   - A word that is **entirely uppercase and ≥ 2 characters** (heuristic
     for an acronym, e.g. `"API"`, `"CI"`) is preserved as-is.
   - Otherwise: first character upper-cased, remainder lower-cased (e.g.
     `"validation"` → `"Validation"`).
   - A purely numeric word (e.g. `"2"` from `"2FA Setup"`) is kept
     unchanged.
4. **Concatenate** all cased words with no separator.
5. **Non-representable input fallback**: if step 1–4 yields an empty
   string, or the result doesn't match `^[A-Za-z][A-Za-z0-9]*$` (e.g. the
   block name was entirely emoji or a non-Latin script with no ASCII
   decomposition), fall back to `Block{first_task_id}` (e.g. `Block091`) —
   always valid, always traceable via the task ID already present in the
   branch-name prefix, and this fallback path is exercised in tests (§18).

**Scope note on internationalization**: this rule deliberately only
transliterates Latin-script diacritics; non-Latin scripts (Cyrillic,
Japanese, etc.) without an ASCII decomposition fall through to the
`Block{id}` fallback rather than attempting a lossy transliteration
scheme — a documented v1 limitation, not an oversight, per the instruction
not to overcomplicate internationalization.

**Alternatives considered**:
- **Fully strip non-ASCII with no fallback (allowing an empty branch
  segment)** — rejected: `T091-` with an empty name is an invalid/ambiguous
  branch name; the `Block{id}` fallback guarantees validity unconditionally.
- **Attempt phonetic transliteration for non-Latin scripts** — rejected as
  overengineering for v1; explicitly out of scope per the planning brief.

---

## 17. Installation / bootstrap / update model — MUST

**Decision**:

1. **`uv` prerequisite**: the one thing that must exist on the machine
   before anything else — installed via the official Astral installer for
   the host OS (exact command verified against `uv`'s official docs at
   documentation-writing time, not fabricated here).
2. **No separate Python prerequisite**: `uv` provisions Python 3.12 itself
   (`uv python install`, or automatically on first `uv run`/`uv sync`
   against a project pinning `.python-version`) — directly satisfying
   FR-038's "must not assume the target OS already provides a suitable
   Python installation."
3. **Installing the runner** (private repo, so GitHub auth is a
   prerequisite per FR-037): the recommended path is
   `uv tool install --from git+https://github.com/fabiofilz/solari-ai-speckit-workflow@<tag> solari-workflow`,
   which places an isolated `solari-workflow` executable on PATH backed by
   its own `uv`-managed environment — fully separate from whatever
   environment the *consuming* project itself uses (Go, Node, Rust,
   Python, or otherwise), satisfying the isolation requirement without
   leaking into the consuming project's own toolchain.
   A local-clone alternative (`uv run --project <path-to-runner>
   solari-workflow ...`) remains available for contributors working on the
   runner itself.
4. **Adopting an existing/new project**: `solari-workflow init` (a planned
   CLI subcommand) detects/prompts for the project's stack, scaffolds
   `.ai-workflow.toml` from a template (values supplied through the
   orchestrator's own dialogue, never guessed by the runner — FR-015/FR-016
   ownership stays with ChatGPT), and ensures `.ai-runs/` and the lock file
   are present in `.gitignore`. Spec Kit's own lifecycle (`specify init`,
   `/speckit.*`) is invoked separately and reused as-is (FR-013), never
   reimplemented by `init`.
5. **Recording workflow version**: `.ai-workflow.toml`'s `[workflow]`
   table records `source` (this repository's URL) and `version` (the
   tagged release the project was bootstrapped/last-upgraded against).
6. **Upgrades are explicit, never silent**: upgrading means re-running `uv
   tool install --from git+...@<new-tag> solari-workflow --force` (or
   updating the pinned ref) and then deliberately bumping `[workflow]
   version` in the project's own config. A mismatch between the installed
   runner's own version and the project's declared `[workflow] version` is
   surfaced as an **informational** warning at readiness-gate time (never
   auto-corrected, never blocking by default in v1 — a future major-version
   migration note may change this per the constitution's Governance
   section).

**Alternatives considered**:
- **A separate bootstrap shell script per OS** — rejected: `uv tool
  install --from git+...` already is that cross-platform bootstrap
  primitive; a bespoke script would duplicate what `uv` does natively.
- **Vendoring/copying the runner into each consuming project** — rejected
  outright: the spec's own Assumptions explicitly require referencing a
  specific version of the canonical repository rather than a
  drift-prone vendored copy.
- **Auto-updating the installed runner** — rejected: contradicts "future
  workflow upgrades are explicit rather than silent," and Principle VII's
  general stance against unattended automation.

---

## 18. Workflow self-testing — MUST (unit/config/fingerprint/git/locking/
readiness), SHOULD (cross-platform CI matrix), DEFERRED (real paid-model
integration checks)

**Decision**: `pytest` (dev-only, §3), organized as:

- **Unit tests** (`tests/unit/`): config load/validate (many valid and
  invalid fixture files, asserting exact aggregated error sets — §5);
  PascalCase derivation (§16) across a table covering spaces, hyphens,
  punctuation, acronyms, non-ASCII/diacritics, and the empty-result
  fallback; slug generation (§8) sharing the same normalization routine;
  Candidate Tree construction and fingerprint computation (§12) —
  deterministic for fixed inputs, changes when a tracked or untracked
  file's content/mode changes (including a symlink case), unaffected by
  changes to Git-ignored paths, and provably never touches the real
  `.git/index` (assert the real index's own `write-tree` OID is unchanged
  before/after a Candidate Tree build); branch-name/tag regex parsing;
  the block-state vocabulary's transition rules (§0.7) and the fixed
  single-retry counter (§0.6).
- **Git workflow tests** (`tests/integration/`): run against **real `git`
  CLI** in temporary repositories (`tempfile.TemporaryDirectory()` + `git
  init`) — not a mocked Git — exercising branch creation via `start-block`,
  the real staging-area precondition (§0.8, including the case where a
  stray `git add` before `checkpoint` is correctly refused without
  mutating the index), `merge --no-ff`, annotated tag creation and
  content, retention deletion (including the "never touch the active
  branch" and "never force-delete" rules), and the full invariant chain
  end-to-end: build Candidate Tree → gate PASS → dirty the working tree
  between gate and checkpoint → assert the exact
  `CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE` message → revert
  → re-gate → checkpoint succeeds → assert `commit^{tree}` equals the
  gated `candidate_tree_oid`. Skipped with a clear reason if `git` is not
  found on `PATH` (expected to always be present in CI).
- **Resume tests**: simulate an operational failure at each
  `last_safe_stage` value (§0.7) by killing the fake-CLI subprocess
  mid-invocation, assert the single automatic retry fires exactly once
  and then `BLOCKED_MANUAL` is recorded, then assert `resume` revalidates
  state and continues from the correct stage **without** re-invoking the
  fake Claude stub when `last_safe_stage` was already
  `IMPLEMENTATION_COMPLETE` or later.
- **Locking tests**: a single-process acquire/release cycle, plus a
  two-process test that spawns a real second `python -m solari_workflow`
  (or a tiny helper script) holding the lock while the main test process
  asserts the second acquisition attempt is refused with the expected
  message shape.
- **Fake/stub `claude`/`codex` CLIs** (`tests/fixtures/fake_claude.py`,
  `fake_codex.py`): standalone scripts invoked via `sys.executable
  <script>` (no shell dependency) that parse a minimal subset of expected
  argv, emit canned stdout — including a controllable `STATUS:
  COMPLETE|ARCHITECTURAL_DECISION_REQUIRED|BLOCKED` marker for the fake
  Claude (§9) and a controllable `RESULT: PASS|FAIL`/findings block for
  the fake Codex (§10) — and exit with a controllable code (including a
  simulated transient launch failure, to exercise the single-retry path,
  §0.6). Tests point the runner at these via an executable-path override
  so no real, paid model call is ever made in the standard suite (an
  explicit requirement).
- **Readiness tests** (`tests/fixtures/projects/`): minimal fixture trees
  for Python/Node/Go/Rust (manifest present/absent, lockfile
  present/absent/untracked variants) run through `readiness/engine.py`,
  asserting the correct `PASS`/`FAIL`/`SKIPPED` per section, including that
  an absent Docker/local-model section never produces `FAIL`.
- **Cross-platform behavior — SHOULD**: a CI matrix (e.g. GitHub Actions
  `ubuntu-latest` / `windows-latest` / `macos-latest`) running the full
  suite on all three, since `pathlib` behavior, path separators, and
  subprocess argv quoting differ subtly enough between Windows and POSIX
  that local emulation of "cross-platform" claims isn't trustworthy on its
  own — this is inexpensive to set up and materially de-risks §6/§7's
  cross-platform claims, so it is SHOULD-for-v1, not DEFERRED.
- **Real, paid `claude`/`codex` CLI calls — explicitly DEFERRED** from the
  standard automated suite: an optional, clearly separate, opt-in-only
  manual verification script may exist later for a human to occasionally
  sanity-check real CLI integration; it is never part of the default
  `pytest` run and is not designed further in this plan.

**Alternatives considered**:
- **Mocking `git` entirely with a fake implementation** — rejected: Git's
  own behavior (merge conflict detection, `branch -d`'s merged-check,
  exact `git diff --binary` output shape) is exactly what needs to be
  proven correct; a hand-written fake would only test the runner's
  assumptions about Git, not Git's actual behavior.
- **Running the standard suite against real Claude/Codex CLIs** —
  rejected outright per the explicit requirement for no real, paid model
  calls in normal automated testing.
