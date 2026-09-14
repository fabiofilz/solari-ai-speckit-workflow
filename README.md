# solari-ai-speckit-workflow

Canonical, versioned, cross-project workflow for AI-assisted software
development built around **Spec Kit**, **ChatGPT**, **Claude Code**, and
**Codex**, with disciplined Git branching and checkpointing.

> **Status**: v1 is under active implementation. The formal specification,
> architecture, bootstrap foundation, Candidate Tree construction,
> validated-state fingerprinting, persistent block state, configuration
> handling, run locking, Git repository inspection, and foundational
> readiness capabilities are implemented through Spec Kit tasks T001–T032
> and have passed independent review. The full orchestration, actor execution,
> checkpoint, retention, resume, and stack-specific readiness lifecycle is
> still being completed. The runner is implemented in Python 3.12.14 and
> managed with `uv`; consuming projects remain technology-agnostic. See
> [`specs/001-workflow-v1/spec.md`](specs/001-workflow-v1/spec.md) for the
> authoritative requirements and
> [`.specify/memory/constitution.md`](.specify/memory/constitution.md) for
> the durable principles governing this workflow.

## Purpose

Shipping software with an AI architect, an AI developer, and an AI reviewer
only stays trustworthy if their roles never blur and every checkpoint is
provably the state that was actually reviewed. This repository is the single
source of truth for that discipline — a project-agnostic workflow that any
project, in any language or stack, can bootstrap onto without forking or
duplicating its rules.

## Actor model

This workflow has **six** distinct components — the User and ChatGPT are
not the same actor, and the Python runner is not the orchestrator:

```
User            → REQUESTS / AUTHORIZES
ChatGPT/Orchestrator → DECIDES / INSTRUCTS
Python Runner   → EXECUTES / VERIFIES / GUARANTEES
Claude Code     → IMPLEMENTS
Codex           → VALIDATES
Spec Kit        → DEFINES THE FORMAL CONTRACT
```

| Actor | Role | Owns |
|---|---|---|
| **User** | Human requester/authorizer | Requesting work ("implement the next block" / "the next N"), authorizing how many blocks a run may cover, manual intervention and authorizing resume when the workflow stops, authorizing an eventual push |
| **ChatGPT** | Architect / Planner / Orchestrator | Interpreting the User's request; architecture decisions, block boundaries, tool/model/effort selection, session NEW vs CONTINUE decisions, acceptance criteria, remediation-vs-checkpoint decisions, workflow policy |
| **Python Runner** | Deterministic executor | The **exclusive** interface between this workflow and Git: repository inspection, the orchestrator-authorized branch's creation, checkpoint staging, the checkpoint commit, `merge --no-ff`, tag creation, branch retention; enforces preconditions/postconditions and fails closed on ambiguous state |
| **Spec Kit** | Formal Development Contract | Specification, clarification, technical planning artifacts, task decomposition |
| **Claude Code** | Developer / Builder | Implementation of the assigned block, implementation-related tests/docs, self-validation — using file edits only, **never** a Git command |
| **Codex** | Independent Reviewer / QA | Independent code review, testing, regression review, invariant verification, determinism checks, scope compliance, severity-classified findings — **never** a Git command |

No actor may silently assume another's authority:

- The User is never required to manually coordinate internal mechanics
  (branch creation, invoking Claude/Codex, checkpointing) — those are the
  Orchestrator's and Runner's job once a block count is authorized.
- Spec Kit decomposes work into tasks but never decides block boundaries or
  orchestration strategy.
- Claude Code implements only its assigned scope; when an out-of-scope
  architectural decision is required, it stops and reports rather than
  deciding. Claude Code never executes a Git command — no `status`,
  `diff`, `add`, `commit`, `checkout`, `branch`, `merge`, `tag`, `push`, or
  anything else, under any circumstances.
- Codex gates are **read-only** — Codex never fixes implementation during a
  gate, and never executes a Git command either. A finding is remediated
  by Claude Code and independently re-gated.
- The Runner never decides block boundaries, task order, architecture,
  model/effort/session choice, or whether execution continues beyond what
  the User authorized — it only executes and verifies what the
  Orchestrator instructs.
- Workflow policy and project configuration are never silently changed by
  Claude Code or Codex; only the orchestrator decides such changes.

## Spec Kit's role

Spec Kit is the formal backbone of *what* is being built. The typical
lifecycle:

```
idea
  → /speckit.specify
  → /speckit.clarify
  → /speckit.plan
  → Environment Contract + project workflow configuration
  → Project Readiness Gate
  → /speckit.tasks
  → ChatGPT selects a coherent task block
  → implementation / review / checkpoint cycles
```

Spec Kit task IDs are the traceability basis for every development branch and
checkpoint tag this workflow creates. A project that already has Spec Kit
artifacts resumes from its current stage; a new project bootstraps the full
lifecycle.

## Lifecycle of one development block

1. **Selection** — a User request ("implement the next block") is
   interpreted by ChatGPT, which selects a coherent range of Spec Kit
   tasks as one block and decides tool/model/effort/session-mode for the
   run.
2. **Readiness Gate + branch creation** — the runner checks the project's
   configuration, Environment Contract, Spec Kit artifacts, and Git state
   against the applicable conditions for that project's stack, then — the
   runner, and only the runner — creates the dedicated branch
   `T{first_task}-{PascalCaseBlockName}`.
3. **Implementation** — Claude Code implements and self-validates the
   block on that branch, using ordinary file edits only; Claude never
   executes a Git command of any kind.
4. **Independent review** — Codex, in a separate, non-concurrent execution,
   reviews the block read-only and reports severity-classified findings.
   Codex, like Claude, never executes a Git command; any repository
   context it needs is prepared and supplied by the runner.
5. **Remediation (if needed)** — Claude Code fixes findings on the *same*
   branch, still without touching Git; Codex re-gates independently. This
   cycle repeats until PASS or the orchestrator decides otherwise.
6. **Checkpoint** — once Codex reports PASS with no blocking severity, the
   runner validates a working-tree fingerprint against what was gated,
   performs the checkpoint commit itself, merges with `git merge --no-ff`
   into main, and creates an annotated tag `checkpoint-T{first}-T{last}` —
   all runner-owned Git operations; neither Claude nor Codex participates.
7. **Stop** — the workflow does not automatically begin the next block.
   ChatGPT decides when and what block starts next, and only in response
   to a User request authorizing further blocks.

Automation only ever handles the mechanics of each stage — it never chains
stages together without an orchestration decision point.

## Strict sequential execution

Claude Code and Codex must never operate concurrently on the same working
tree. Only one workflow execution may own a project's working tree at a
time. This is a technical guarantee the runner is required to enforce (e.g.
via a run lock), not a convention documented and hoped for.

## Environment Contract

Every consuming project declares enough environment information to
reconstruct its supported development/runtime environment, including (where
applicable to that project's stack):

- Language/runtime and version; dependency manager and manifest; a
  **committed** dependency lockfile.
- Docker/Compose components with **pinned, non-`latest`** image tags.
- Direct OS-installed dependencies with exact/supported versions.
- Reproducible installation instructions (README or designated docs).
- Local services; Ollama/local-model version and digest/hash where the
  platform exposes one.
- Architecture/OS assumptions where materially relevant.
- Environment variables via safe examples only — **never** committed
  secrets.

A single source of truth is preferred over duplication: if a Docker image tag
is authoritative in Compose, documentation references Compose instead of
manually restating the version.

## Project Readiness Gate

Before a block's first implementation, the workflow checks (conditionally,
based on the project's actual stack — an absent section is skipped, not
failed):

- Project workflow config exists and is valid.
- Required Spec Kit artifacts exist.
- Runtime policy is defined; dependency manifest exists; required lockfile
  exists **and is tracked by Git**.
- Configured test/build/lint commands are defined where applicable.
- Docker image tags are pinned when applicable.
- OS-installed prerequisites are documented when applicable.
- Local-model/Ollama requirements are documented/pinned when applicable.
- `.ai-runs/` is excluded from Git.
- Main branch is identified; checkpoint strategy is defined.
- No secrets are knowingly present in workflow configuration.

## Branch naming

Development branches: `T{first_task}-{PascalCaseBlockName}`

```
T091-ValidationPipeline
T100-ValidationReporting
```

The numeric portion is the first Spec Kit task ID in the block — it is not an
independent counter. A branch represents the *complete* work needed to finish
and approve its block: Codex findings and their remediation stay on the same
branch, even when a fix touches code from an earlier block. A new branch
begins only when a new block is formally selected.

## Independent review & remediation

Codex gates are independent (separate session from Claude Code, never
concurrent) and read-only. A finding is remediated by Claude Code and then
independently re-gated — the same actor never both fixes and re-approves its
own work in the same gate.

## Checkpoint & tag strategy

- Checkpoint only after an independent Codex gate reports **PASS** with no
  unresolved blocking severity.
- A **validated-state fingerprint** proves the checkpointed state is the
  exact state Codex reviewed (mechanism finalized during technical planning).
- Merge with `git merge --no-ff` into the configured main branch — no
  squash, no rebase, by default.
- No push unless explicitly requested/authorized.
- Annotated tag `checkpoint-T{first_task}-T{last_task}` (e.g.
  `checkpoint-T091-T099`) records:
  - **Required**: block name, branch name, task range with each task ID and
    title, independent gate result (PASS), checkpoint status (VERIFIED).
  - **Optional, best-effort, only if already available from the final
    gate**: test counts, known expected/deferred failures, lint result,
    determinism result. No extra work is ever performed solely to enrich
    this optional metadata.

## Branch retention

Checkpoint tags are permanent. Merged development branches are temporary:
the default policy keeps the two most recently merged branches plus the
currently active branch; older merged branches may be safely (non-force)
removed. Retention applies prospectively only — it never rewrites or prunes
history that predates the policy.

## `.ai-runs/`

Each consuming project's `.ai-runs/` directory holds operational execution
history (prompt/result files) and stays **outside Git**. Naming follows:

```
YYYYMMDD-NNN-slug-prompt.md
YYYYMMDD-NNN-slug-result.md
```

Existing run files are never overwritten or silently renamed.

## Installation / bootstrap

> The bootstrap process below describes the intended complete v1 workflow.
> The runner is partially implemented; capabilities that depend on unfinished
> lifecycle tasks must still be treated as target behavior until those tasks
> are implemented and independently validated.

1. Ensure GitHub access to this repository — it is currently **private**, so
   the machine running Claude Code needs an authenticated `gh`/git
   credential with read access before it can be cloned or referenced.
2. Ensure the supported Python version and `uv` are installed on the machine
   that will run the workflow runner (macOS, Windows, or Linux where
   practical). Do not assume the OS already provides a suitable Python — the
   exact supported Python version is frozen during technical planning, and
   bootstrap documentation will verify/document both prerequisites
   explicitly. The runner uses an isolated, reproducible environment (via
   `uv`) rather than depending implicitly on an OS-managed Python.
3. Reference this repository (and a specific released version, e.g.
   `1.0.0`) from the consuming project rather than vendoring a copy that can
   drift.
4. Have ChatGPT generate the project's workflow configuration and
   Environment Contract for that project's actual stack.
5. Run the Project Readiness Gate; resolve any reported gaps.
6. Proceed through the Spec Kit lifecycle (or resume an existing one) and
   begin block execution.

> **Note**: The runner implementation language/environment (Python + `uv`)
> is a decision about this workflow repository only. Consuming projects
> remain technology-agnostic — their own runtime, language, and dependency
> choices are defined by that project's architecture, Spec Kit artifacts,
> and Environment Contract, independent of the runner's implementation.

Do not assume a global `~/.claude/skills/solari-speckit-executor/` skill
already exists on the target machine — v1's design does not depend on it.

## Using this workflow in a new project

Start from an empty or near-empty repository, bootstrap Spec Kit (constitution
→ specify → clarify → plan → tasks), generate the project workflow
configuration and Environment Contract, pass the Readiness Gate, then begin
block-by-block execution as described above.

## Using this workflow in an existing Spec Kit project

Reuse whatever specification/plan/task artifacts already exist. Add only the
missing pieces: project workflow configuration, Environment Contract,
Readiness Gate, `.ai-runs/` exclusion, and branch/checkpoint conventions —
then resume the lifecycle from the first block that has not yet been
checkpointed under this workflow.

## Limitations / prerequisites (v1 implementation stage)

- The runner foundation is implemented through Spec Kit tasks T001–T032 and
  has passed independent review.
- Python 3.12.14 is the pinned runner runtime, managed reproducibly with `uv`.
- Candidate Tree construction, validated-state fingerprinting, persistent
  block state, run locking, configuration handling, Git repository inspection,
  and foundational readiness behavior are implemented.
- The full single-block lifecycle (T033–T050) is implemented and tested
  end-to-end against fake `claude`/`codex` CLI stubs: `start-block`,
  `run-claude`, `run-codex-gate`, `checkpoint` (commit-tree/update-ref +
  `merge --no-ff` + annotated tag), `resume` (revalidation only — it never
  auto-invokes the next stage itself, per Controlled Automation), and
  `status`. The single-retry-then-`BLOCKED_MANUAL` policy is implemented at
  the actor-invocation level (a `claude`/`codex` launch failure); it is not
  yet implemented for a transient failure inside a Git operation itself
  (`start-block`'s or `checkpoint`'s own Git calls), which remains a
  documented gap rather than a silently assumed guarantee.
- Concurrency enforcement (wiring the run lock into every subcommand's own
  dispatch, User Story 3), stack-specific readiness checks (User Story 4),
  and branch retention (User Story 5, `checkpoint`'s own retention call site
  is left explicit and unfilled) are not yet implemented.
- The current bootstrap implementation checkpoint predates the runner's own
  complete checkpoint lifecycle and is therefore recorded as an explicit
  bootstrap exception.
- Consuming projects remain technology-agnostic.
- This workflow assumes Git-based projects; non-Git projects are out of scope.

## Upgrading workflow versions

This repository is versioned with semantic versioning (e.g. `1.0.0`). Each
consuming project's workflow configuration should record the workflow
version it was bootstrapped against. Upgrading a consuming project to a
newer workflow version is a deliberate, ChatGPT-directed action — never an
automatic or silent update. Release automation for this repository is out of
scope for v1.

## Repository structure

See [`docs/architecture/repository-structure.md`](docs/architecture/repository-structure.md)
for the proposed, architecture-level layout of this repository (config
schema, templates, and the future runner location) as designed for v1.

## Contributing / governance

Durable principles are recorded in
[`.specify/memory/constitution.md`](.specify/memory/constitution.md).
Changes to workflow policy affecting consuming projects require an explicit
version bump and, where relevant, a migration note.
