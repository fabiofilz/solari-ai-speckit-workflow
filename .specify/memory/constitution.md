# Solari AI Spec Kit Workflow Constitution

## Core Principles

### I. Explicit Actor Separation (NON-NEGOTIABLE)
The workflow defines four distinct roles — ChatGPT (Architect/Planner/Orchestrator),
Spec Kit (Formal Development Contract), Claude Code (Developer/Builder), and Codex
(Independent Reviewer/QA) — and no actor may silently assume another actor's
authority. ChatGPT alone owns architecture decisions, block boundaries,
tool/model/effort selection, session NEW vs CONTINUE decisions, acceptance
criteria, and remediation-vs-checkpoint decisions. Spec Kit owns specification,
clarification, planning artifacts, and task decomposition, but never decides
block boundaries or orchestration strategy. Claude Code implements only the
scope explicitly assigned to it, and must stop and report when an out-of-scope
architectural decision is required rather than deciding it silently. Codex
performs independent, read-only review, testing, and severity-classified
findings, and must never fix implementation during a gate. Workflow policy and
project configuration may be reported as needing change by Claude or Codex, but
only ChatGPT/the orchestrator may decide and apply that change.

### II. Specification Before Implementation
No implementation block begins without a Spec Kit specification, clarified
scope, a plan, and task decomposition covering that block. Spec Kit task IDs
are the traceability basis for Git development branches and checkpoints:
every development branch and checkpoint tag must be attributable to specific
task IDs. Projects with pre-existing specification artifacts may resume from
their current lifecycle stage; new projects must bootstrap the full lifecycle
(specify → clarify → plan → tasks) before a block is implemented.

### III. Independent Review Before Checkpoint (NON-NEGOTIABLE)
No block may be checkpointed (merged and tagged) without an independent Codex
gate reporting PASS with no unresolved severity that blocks freezing under
workflow policy. A finding raised during a gate is remediated by Claude Code
and then independently re-gated by Codex — the same actor never both fixes and
re-approves its own or another actor's work in the same gate. Findings,
remediation, and re-gate all remain on the same block's development branch;
they never silently become a new block or a new branch.

### IV. Sequential Execution (NON-NEGOTIABLE)
Claude Code and Codex must never operate concurrently on the same working
tree. Only one workflow execution may own the project working tree at a time.
This is a technical guarantee the runner must enforce (e.g. via a run lock),
not merely a documented convention. A new coherent development block normally
starts a NEW Claude session; independent Codex gates always use a NEW session;
immediate remediation may continue a Claude session only when the orchestrator
explicitly chooses to do so. The system must never blindly resume a stale
session.

### V. Reproducible Environments
Every consuming project must define an Environment Contract sufficient to
reconstruct its supported development/runtime environment: language/runtime
and version, dependency manager and manifest, a committed dependency lockfile,
pinned (non-`latest`) container image tags where containers are used,
documented direct-OS dependencies with exact/supported versions, documented
local services, and pinned local-model/Ollama version and digest where
applicable. A single authoritative source of truth is preferred over
duplicated version declarations (e.g. Compose is authoritative over a manually
restated version in prose). Environment variables are documented via safe
examples only; secrets are never committed. Before any block's first
implementation, a Project Readiness Gate verifies the applicable subset of
this contract — checks are conditional on the project's actual stack and never
fail solely because an inapplicable section (e.g. no Docker, no local LLM) is
absent.

### VI. Auditable Checkpoints
A checkpoint is a verified, permanent historical fact, not a convenience
commit. Before checkpoint creation, the workflow must be able to prove that
the working-tree state being merged is the exact state Codex reviewed, via a
validated-state fingerprint mechanism (implementation decided during
planning). Checkpoint integration always uses `git merge --no-ff` into the
configured main branch, preserving branch topology; squash and rebase
checkpoints are not used by default. After a verified merge, an annotated tag
`checkpoint-T{first_task}-T{last_task}` records required metadata (block name,
branch name, task range with each task ID/title, independent gate result:
PASS, checkpoint status: VERIFIED) and, only when already available from the
final gate, optional metadata (test counts, known expected/deferred failures,
lint result, determinism result). No additional test, model, or search work is
performed solely to enrich tag metadata. No push occurs unless explicitly
requested/authorized.

### VII. Controlled Automation
Automation handles execution mechanics; it never chains
implementation → review → remediation → review → checkpoint → next block
without an orchestration decision point. Required stopping points exist after
at minimum: implementation/self-validation, the independent gate, any
remediation/re-gate cycle, checkpoint preparation, and a completed checkpoint
before the next block may begin. ChatGPT retains every architectural and
progression decision; the runner must never advance past a stopping point on
its own initiative.

### VIII. Cross-Project Portability
This repository is the canonical, versioned source for the workflow across
projects and technology stacks. Nothing in the workflow's schema, scripts, or
templates may be coupled to any single consuming project (including the
original `solari-ai-pdf-document-converter` reference project). Every
consuming project supplies its own project-level workflow configuration
(owned/generated by ChatGPT) that parameterizes project name, artifact type,
main branch, Spec Kit paths, Environment Contract, and policy — the generic
schema itself carries no project-specific data. The runner's implementation
technology must be evaluated (not assumed) against macOS, Windows, and Linux
support, minimal bootstrap dependencies, and the ability to invoke Claude Code
and Codex CLIs, before it is chosen.

### IX. Minimal, Justified Dependencies
Every dependency the runner or its scripts introduce must be justified against
the portability and maintainability goals of this workflow. Prefer the
smallest dependency set capable of: invoking external CLIs, file
locking/single-run ownership, Git operations, hashing/fingerprinting, and
config parsing/validation. A dependency that only saves effort but narrows
platform support or install simplicity requires explicit justification in the
technical plan.

## Branch, Checkpoint, and Retention Policy

Development branches use `T{first_task}-{PascalCaseBlockName}` (e.g.
`T091-ValidationPipeline`), where the numeric portion is the first Spec Kit
task ID in the block — it is not an independent branch sequence. A branch
represents the complete work needed to finish and approve its block: findings
and remediations discovered by Codex, including fixes that touch code from an
earlier block, remain on the same block branch and never spawn a new branch.
A new branch begins only when a new development block is formally selected.

Checkpoint tags (`checkpoint-T{first_task}-T{last_task}`) are permanent
historical references and are never deleted or rewritten. Merged development
branches are temporary convenience references: the default retention policy
keeps the two most recently completed/merged development branches plus the
currently active development branch; older merged branches may be safely
removed according to policy, never force-deleted merely to satisfy retention,
and retention applies prospectively only — historical branches are never
retroactively rewritten or pruned to comply with a policy adopted later.

## Governance

This constitution supersedes ad hoc practice for any project bootstrapped
against this workflow. Amendments require an explicit version bump, a
documented rationale, and — where the change affects consuming projects — a
migration note describing how already-bootstrapped projects adopt the change.
Workflow configuration and policy in a consuming project must never be
silently changed by Claude Code or Codex during ordinary implementation or
review; only ChatGPT/the orchestrator may decide and apply such a change,
based on a reported need. All specifications, plans, and task breakdowns
produced under this workflow must be checked against these principles;
complexity beyond what a principle allows must be justified in the plan.

**Version**: 1.0.0 | **Ratified**: 2026-09-12 | **Last Amended**: 2026-09-12
