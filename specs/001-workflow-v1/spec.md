# Feature Specification: AI Spec Kit Workflow v1

**Feature Branch**: `001-workflow-v1`

**Created**: 2026-09-12

**Status**: Draft

**Input**: User description: "Solari AI Spec Kit orchestration workflow v1: ChatGPT-orchestrated, Spec-Kit-driven, Claude Code and Codex sequential execution with readiness gates, checkpoints, and cross-project portability"

## Clarifications

### Session 2026-09-12

- Q: What format should the project-level workflow configuration use? → A: **Human-readable, format decided during planning** unless planning identifies a better alternative (e.g. YAML/TOML/structured Markdown), consistent with the runner-technology choice; no format is locked in this specification (see FR-015, FR-017, Assumptions).
- Q: What technology should implement the future runner? → A: **Decided: Python, managed with `uv`.** This is a v1 architectural decision, not an open question. Supported target operating systems are macOS, Windows, and Linux where practical; the OS is not assumed to already provide a suitable Python installation, so installation/bootstrap documentation must explicitly verify/document the supported Python and `uv` prerequisites, and the runner must use an isolated/reproducible environment rather than depend implicitly on an OS-managed Python. The exact supported Python version is selected/frozen during technical planning (see FR-038). This decision applies only to the runner implementation; consuming projects remain technology-agnostic (see Assumptions).
- Q: Does v1 implement the runner, Codex integration, `.ai-runs/` automation, or Git/checkpoint automation? → A: **No.** This is a bootstrap-and-specification stage only; FR-005, FR-023, FR-029, FR-032, and related requirements define required future behavior that the technical plan and subsequent implementation tasks must satisfy, but no runner code is produced under this specification.
- Q: Is the legacy global skill (`~/.claude/skills/solari-speckit-executor/SKILL.md`) a dependency of this workflow? → A: **No.** It is treated as legacy/reference only; this specification and its future implementation must not assume its presence on any machine.
- Q: Does this workflow reimplement Spec Kit's specify/clarify/plan/tasks mechanics? → A: **No.** Spec Kit's existing CLI, scripts, and templates are reused as-is; this workflow only defines the orchestration, configuration, gating, and checkpoint layer around Spec Kit's existing lifecycle commands.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Bootstrap the workflow onto a new project (Priority: P1)

A project owner points a fresh, empty (or pre-existing) project at this
workflow repository and, following its documented bootstrap process, ends up
with Spec Kit initialized, a project-level workflow configuration generated
by ChatGPT, and an Environment Contract capturing the project's actual stack
— without any of it referencing the original PDF Converter reference project.

**Why this priority**: Without a working, generic bootstrap, the workflow
cannot be reused anywhere and remains a single-project artifact.

**Independent Test**: Can be fully tested by applying the documented bootstrap
steps to a throwaway project of a different technology stack (e.g. a Go
project) and confirming the resulting project configuration and Environment
Contract contain no PDF-Converter-specific values and no hard failures from
inapplicable sections (no Docker, no local LLM).

**Acceptance Scenarios**:

1. **Given** a project with no prior Spec Kit or workflow artifacts, **When**
   the bootstrap process is followed, **Then** the project ends up with Spec
   Kit initialized, a project workflow configuration file, and an Environment
   Contract, all generated for that project's actual stack.
2. **Given** a project that already has Spec Kit specification/plan/task
   artifacts, **When** the bootstrap process is followed, **Then** the
   existing artifacts are reused as-is and only the missing workflow
   configuration/Environment Contract/readiness-gate pieces are added.

---

### User Story 2 - Run one block through the Readiness Gate, implementation, independent review, and checkpoint (Priority: P1)

An orchestrator (ChatGPT) selects a coherent Spec Kit task block, the
Readiness Gate confirms the project is prepared, Claude Code implements and
self-validates the block on a dedicated branch, Codex independently reviews
it in a separate, non-concurrent execution, any findings are remediated by
Claude Code and re-gated by Codex, and — only after an independent PASS — the
block is checkpointed with a fingerprint-verified, no-fast-forward merge and
an annotated tag.

**Why this priority**: This is the core value proposition of the workflow:
disciplined, auditable, role-separated delivery of a unit of work.

**Independent Test**: Can be fully tested by running one small, self-
contained task block end-to-end and inspecting that: a branch named
`T{first_task}-{PascalCaseBlockName}` was used, Codex's gate ran only after
Claude's session ended, a checkpoint tag `checkpoint-T{first}-T{last}` exists
with the required metadata, and the working tree matches the fingerprint
recorded at gate time.

**Acceptance Scenarios**:

1. **Given** a project that passes the Readiness Gate, **When** a task block
   is selected and implemented, **Then** a development branch named
   `T{first_task}-{PascalCaseBlockName}` is used for all work belonging to
   that block.
2. **Given** Claude Code has finished implementing and self-validating a
   block, **When** the independent Codex gate runs, **Then** it runs in a
   session and execution separate from Claude's, never concurrently with it,
   and never modifies implementation code itself.
3. **Given** Codex reports findings, **When** Claude Code remediates them,
   **Then** the remediation stays on the same block branch and Codex re-gates
   independently before any checkpoint is considered.
4. **Given** an independent gate reports PASS with no blocking severity,
   **When** a checkpoint is prepared, **Then** the workflow verifies the
   working-tree state matches what was gated (via a fingerprint) before
   merging with `git merge --no-ff` and creating an annotated
   `checkpoint-T{first}-T{last}` tag carrying the required metadata.
5. **Given** a checkpoint has just completed, **When** no further explicit
   orchestrator instruction has been given, **Then** the workflow does not
   automatically begin the next block.

---

### User Story 3 - Prevent concurrent or unauthorized execution (Priority: P1)

While Claude Code or Codex is executing against a project's working tree, any
attempt to start the other (or another instance of the same actor) against
the same working tree is blocked until the first execution ends.

**Why this priority**: Concurrent writers/readers against the same working
tree during implementation or review would silently corrupt the audit trail
this workflow exists to guarantee.

**Independent Test**: Can be fully tested by starting one execution, then
attempting to start a second execution against the same project, and
observing the second is refused/queued rather than allowed to run
concurrently.

**Acceptance Scenarios**:

1. **Given** Claude Code is actively executing an implementation block,
   **When** an attempt is made to start a Codex gate on the same working
   tree, **Then** the attempt is refused until Claude Code's execution ends.
2. **Given** no execution currently owns the working tree, **When** a new
   execution starts, **Then** it acquires ownership and records which tool,
   model, effort, session mode, and scope it is running with.

---

### User Story 4 - Validate project readiness before a block starts (Priority: P2)

Before the first implementation of a block, the workflow checks that the
project's configuration, Spec Kit artifacts, Environment Contract, and Git
state satisfy the applicable readiness conditions for that project's actual
stack, and reports exactly what is missing rather than failing silently or
failing on inapplicable checks.

**Why this priority**: Catching a missing lockfile, an unpinned image tag, or
an untracked required file before implementation starts is far cheaper than
discovering it during or after a Codex gate.

**Independent Test**: Can be fully tested by deliberately omitting one
readiness condition (e.g. a required lockfile not committed) and confirming
the gate reports that specific, actionable failure and does not proceed.

**Acceptance Scenarios**:

1. **Given** a project with no Docker components and no local LLM, **When**
   the Readiness Gate runs, **Then** it does not fail on Docker-tag-pinning or
   Ollama-version checks, because those sections do not apply.
2. **Given** a project whose required dependency lockfile exists on disk but
   is not tracked by Git, **When** the Readiness Gate runs, **Then** it
   reports that specific failure and blocks progression to implementation.

---

### User Story 5 - Retain an auditable, bounded history of branches and checkpoints (Priority: P3)

After several blocks have been checkpointed, old merged development branches
beyond the retention policy are identified as safe to remove, while
checkpoint tags and the currently active branch are never touched.

**Why this priority**: Keeps the repository navigable over the life of a
project without ever risking loss of the permanent checkpoint history.

**Independent Test**: Can be fully tested by checkpointing three or more
blocks in sequence and confirming only the two most recent merged branches
plus the active branch remain, while every checkpoint tag from all blocks
still exists.

**Acceptance Scenarios**:

1. **Given** three or more blocks have been merged and checkpointed, **When**
   retention is applied, **Then** only the two most recently merged
   development branches and the currently active branch remain among
   development branches, and no checkpoint tag is ever removed.
2. **Given** a branch is a candidate for removal under retention policy,
   **When** removal is attempted, **Then** it only proceeds as a safe
   (non-force) deletion, and never against the currently active branch.

---

### Edge Cases

- What happens when a consuming project has an Environment Contract section
  that is genuinely inapplicable (e.g. no containers, no local LLM)? The
  Readiness Gate must skip, not fail, that section.
- What happens when Codex finds a blocking issue on re-gate after
  remediation? The block remains on the same branch, unmerged, and the
  orchestrator decides the next step — the workflow does not retry
  automatically.
- What happens when the working tree changes between the Codex gate and
  checkpoint preparation (e.g. a stray manual edit)? The fingerprint
  mismatch must block checkpoint creation until re-gated against the new
  state.
- What happens when `.ai-runs/` numbering would collide with an existing
  prompt/result file? The existing file must never be overwritten or
  silently renamed; the workflow must select the next free number/slug.
- What happens when a project has no prior Spec Kit artifacts at all? The
  workflow must support bootstrapping the full lifecycle from idea through
  tasks, not only resuming an existing one.
- What happens when the orchestrator wants to continue a Claude session for
  immediate remediation? This must be an explicit orchestrator choice per
  execution, never an automatic default, and independent Codex gates must
  always use a NEW session regardless.
- What happens when branch retention would otherwise remove the currently
  active development branch? The active branch is always excluded from
  removal regardless of merge age.
- What happens when a private repository is used from a new machine with no
  prior GitHub authentication? Bootstrap documentation must account for the
  authentication step rather than assuming it is already configured.

## Requirements *(mandatory)*

### Functional Requirements

**Actor model & role boundaries**

- **FR-001**: The workflow MUST define exactly four roles — ChatGPT
  (Architect/Planner/Orchestrator), Spec Kit (Formal Development Contract),
  Claude Code (Developer/Builder), and Codex (Independent Reviewer/QA) — with
  the ownership boundaries described in this specification's actor model.
- **FR-002**: The workflow MUST prevent Claude Code and Codex from silently
  redefining workflow policy, project configuration, or acceptance criteria;
  only the orchestrator may decide such changes, after either actor reports
  the need for one.
- **FR-003**: Claude Code MUST be able to stop and report when it determines
  an out-of-scope architectural decision is required, instead of deciding it
  unilaterally.
- **FR-004**: Codex gates MUST be read-only with respect to implementation:
  Codex may produce severity-classified findings but MUST NOT modify
  implementation files during a gate.

**Sequential execution**

- **FR-005**: The workflow MUST technically prevent Claude Code and Codex
  from operating concurrently on the same project working tree (not merely
  document the restriction).
- **FR-006**: Only one workflow execution MUST be able to own a given
  project's working tree at any time.
- **FR-007**: The workflow MUST support recording, per execution: tool,
  model, effort/reasoning, session mode (NEW or CONTINUE/RESUME), whether
  Spec Kit is involved, scope/task block, and purpose.
- **FR-008**: A new coherent development block MUST default to a NEW Claude
  Code session; an independent Codex gate MUST always use a NEW session.
- **FR-009**: The workflow MUST support an orchestrator-chosen CONTINUE mode
  for immediate remediation of a Claude Code session, and MUST NOT resume a
  stale session automatically or by default.

**Controlled progression**

- **FR-010**: The workflow MUST NOT automatically chain implementation,
  review, remediation, re-review, and checkpoint into a single uninterrupted
  sequence without an orchestration decision point between stages.
- **FR-011**: The workflow MUST stop and await an orchestrator decision at,
  at minimum: after implementation/self-validation, after the independent
  gate, after any remediation/re-gate cycle, after checkpoint preparation,
  and after a completed checkpoint before the next block begins.

**Spec Kit integration**

- **FR-012**: The workflow MUST support projects that already have Spec Kit
  specification, clarification, plan, and/or task artifacts, resuming from
  whatever stage already exists.
- **FR-013**: The workflow MUST support bootstrapping the full Spec Kit
  lifecycle (specify → clarify → plan → tasks) for a project that has none of
  these artifacts yet.
- **FR-014**: Development branches and checkpoint tags MUST be traceable to
  specific Spec Kit task IDs.

**Project configuration & Environment Contract**

- **FR-015**: Each consuming project MUST have its own project-level workflow
  configuration, generated and owned by ChatGPT, in a human-readable format.
- **FR-016**: The project configuration schema MUST be generic and carry no
  project-specific data by default — no field may hard-code values belonging
  to any single reference project.
- **FR-017**: The project configuration MUST conceptually support: workflow
  schema/version, workflow source/version, project name, project
  type/artifact type, main branch, Spec Kit paths/status, runtime/environment
  requirements, dependency manager, dependency manifest, lockfile policy,
  build/test/lint commands, generated/ignored artifacts, protected files,
  model/tool defaults where appropriate, checkpoint policy, branch/tag
  policy, branch retention policy, and project-specific known expected
  failures or deferred owners when explicitly configured.
- **FR-018**: The Environment Contract MUST support, where applicable to a
  given project's stack: language/runtime and version, dependency manager and
  version, dependency manifest, dependency lockfile, a requirement that
  application lockfiles are committed, Docker/Compose components with pinned
  (non-`latest`) image tags, direct OS-installed dependencies with
  exact/supported versions, reproducible installation instructions, local
  services, Ollama version, local LLM/model identity, model version/tag and
  digest/hash when exposed by the platform, architecture/OS assumptions where
  materially relevant, and environment variables via safe examples only
  (never committed secrets).
- **FR-019**: The Environment Contract MUST avoid duplicating an
  authoritative source of truth (e.g., referencing a Docker image tag defined
  in Compose rather than restating it manually elsewhere).

**Project Readiness Gate**

- **FR-020**: Before the first implementation of a block, the workflow MUST
  support a readiness gate that checks: project workflow config exists and
  is valid; required Spec Kit artifacts exist; runtime policy is defined;
  dependency manifest exists; required lockfile exists and is tracked by
  Git; configured test/build/lint commands are defined where applicable;
  Docker image versions/tags are pinned when applicable; OS-installed
  prerequisites are documented when applicable; local-model/Ollama
  requirements are documented/pinned when applicable; `.ai-runs/` is excluded
  from Git; main branch is identified; checkpoint strategy is defined; and no
  secrets are knowingly included in workflow configuration.
- **FR-021**: Readiness Gate checks MUST be conditional on the project's
  actual stack; a check for a section the project does not use (e.g. no
  Docker, no local LLM) MUST be skipped, not failed.

**Execution history (`.ai-runs/`)**

- **FR-022**: Each consuming project MUST use a `.ai-runs/` directory for
  operational prompt/result history, and this directory MUST remain excluded
  from Git.
- **FR-023**: The workflow MUST (eventually, at implementation time) support
  automated, unique prompt/result naming compatible with the pattern
  `YYYYMMDD-NNN-slug-prompt.md` / `YYYYMMDD-NNN-slug-result.md`.
- **FR-024**: Existing `.ai-runs/` files MUST never be overwritten or
  silently renamed by the workflow.

**Git branch, checkpoint, and tag conventions**

- **FR-025**: Development branches MUST be named
  `T{first_task}-{PascalCaseBlockName}`, where the numeric portion is the
  first Spec Kit task ID in the block and is not an independent branch
  sequence.
- **FR-026**: A development branch MUST represent the complete work needed to
  finish and approve its block; findings and remediations discovered by
  Codex — including fixes touching code from an earlier block — MUST remain
  on the same block branch rather than creating a new branch.
- **FR-027**: A new development branch MUST begin only when a new
  development block is formally selected by the orchestrator.
- **FR-028**: A block MUST become eligible for checkpoint only after an
  independent Codex gate reports PASS with no unresolved severity that blocks
  freezing under workflow policy.
- **FR-029**: Before checkpoint creation, the workflow MUST validate a
  working-tree/diff fingerprint proving the state being checkpointed is the
  state Codex reviewed.
- **FR-030**: Checkpoint integration MUST use `git merge --no-ff` into the
  configured main branch, preserving branch topology; squash/rebase
  checkpoints MUST NOT be used by default.
- **FR-031**: No push MUST occur as part of a checkpoint unless explicitly
  requested/authorized.
- **FR-032**: After a verified merge, the workflow MUST create an annotated
  tag `checkpoint-T{first_task}-T{last_task}` carrying required metadata:
  block name, development branch name, task range, each included task ID and
  title, independent gate result (PASS), and checkpoint status (VERIFIED).
- **FR-033**: The checkpoint tag MAY additionally carry optional metadata
  (test counts, known expected/deferred failures, lint result, determinism
  result) only when already available from the final gate; the workflow
  MUST NOT perform additional tests, model calls, or searches solely to
  enrich this optional metadata.

**Branch retention**

- **FR-034**: The default branch retention policy MUST keep the two most
  recently completed/merged development branches plus the currently active
  development branch; older merged development branches MAY be safely
  (non-force) removed.
- **FR-035**: Branch retention MUST apply prospectively only; historical
  branches that predate a retention policy MUST NOT be retroactively
  rewritten or pruned to comply with it.

**Portability**

- **FR-036**: The workflow's schema, scripts, and templates MUST remain
  generic across projects and technology stacks; no consuming project's
  specifics (including the original PDF Converter reference project) may be
  hard-coded into the shared workflow repository.
- **FR-037**: The workflow MUST document a bootstrap process usable from a
  machine that does not already have any prior global workflow skill
  installed, and MUST account for GitHub authentication being required
  because this repository is private.

*Clarifications resolved during specification (see Assumptions for defaults
chosen when the source requirements allowed more than one reasonable
interpretation):*

- **FR-038**: The future runner MUST be implemented in **Python** and managed
  with **`uv`**; this is a decided v1 architectural decision, not an open
  question. The runner MUST support macOS, Windows, and Linux where
  practical, and MUST NOT assume the target operating system already
  provides a suitable Python installation — installation/bootstrap
  documentation MUST explicitly verify/document the supported Python and
  `uv` prerequisites, and the runner MUST use an isolated/reproducible
  environment (via `uv`) rather than depend implicitly on an OS-managed
  Python environment. The exact supported Python version MAY be selected/
  frozen during technical planning, which MUST also evaluate HOW to
  implement the Python + `uv` runner: Python version policy, package/project
  layout, cross-platform subprocess handling, file locking/single-run
  ownership, Git operations, hashing/fingerprinting, config parsing/
  validation, CLI integration, and installation/bootstrap/update experience.
  This decision governs only the runner's own implementation; it MUST NOT be
  used to infer or constrain the technology stack of any consuming project
  (see Assumptions).

### Key Entities *(include if feature involves data)*

- **Project Workflow Configuration**: Per-project, ChatGPT-owned settings
  (schema/version, workflow source/version, project identity, Spec Kit
  paths, runtime requirements, dependency policy, build/test/lint commands,
  protected files, checkpoint/branch/retention policy, known deferred
  failures). One instance per consuming project.
- **Environment Contract**: The declared, reconstructible development/runtime
  environment for a project (runtime/version, dependency manager/manifest/
  lockfile, container image tags, OS dependencies, local services, local
  model version/digest, environment variable examples). Embedded in or
  referenced by the Project Workflow Configuration.
- **Readiness Gate Result**: The outcome of evaluating a project's current
  state against the applicable readiness checks for its stack, prior to a
  block's first implementation.
- **Development Block**: A coherent, orchestrator-selected range of Spec Kit
  tasks implemented, reviewed, and checkpointed together on one branch.
- **Execution Record**: A single Claude Code or Codex run's metadata — tool,
  model, effort, session mode, scope/task block, purpose — and its
  prompt/result files under `.ai-runs/`.
- **Working-Tree Fingerprint**: A validated representation of repository
  state at the moment of an independent gate, used to prove the checkpointed
  state matches the gated state.
- **Checkpoint**: A verified `git merge --no-ff` into main plus an annotated
  `checkpoint-T{first}-T{last}` tag with required (and optional, best-effort)
  metadata.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A project of a technology stack other than the PDF Converter's
  can be bootstrapped onto this workflow using only the repository's
  documentation, producing a valid project configuration and Environment
  Contract with zero PDF-Converter-specific values.
- **SC-002**: In 100% of observed block executions, Codex's independent gate
  session starts only after Claude Code's implementation session for that
  block has ended, never concurrently.
- **SC-003**: 100% of checkpoint tags created under this workflow contain all
  required metadata fields (block name, branch name, task range with IDs/
  titles, gate result PASS, checkpoint status VERIFIED).
- **SC-004**: 100% of checkpoints are created only when the working-tree
  fingerprint at checkpoint time matches the fingerprint recorded at the
  passing gate; any mismatch blocks the checkpoint.
- **SC-005**: After N blocks are checkpointed (N ≥ 3), exactly the two most
  recently merged development branches plus the active branch remain among
  development branches, and all N checkpoint tags remain present.
- **SC-006**: A Readiness Gate run against a project lacking Docker and
  local-LLM usage produces zero failures attributable to those sections.
- **SC-007**: No `.ai-runs/` prompt or result file is ever overwritten:
  repeated runs that would collide on name instead produce a new, distinct
  file.

## Assumptions

- The workflow repository (`solari-ai-speckit-workflow`) is the single
  canonical, versioned source of the schema, templates, and (later) runner;
  consuming projects reference a specific workflow version rather than
  vendoring a copy that can drift.
- This specification governs orchestration policy, schema, and required
  behavior. The runner's implementation language and environment manager
  (Python, managed with `uv`) are a decided v1 architectural decision (see
  FR-038); the technical plan evaluates HOW to implement that decision, not
  WHETHER Python/`uv` is the right choice. This decision applies only to the
  workflow runner itself — consuming projects remain technology-agnostic and
  may be implemented in Go, Python, Node.js, Rust, or any other stack; their
  runtime/language/dependency choices belong to that project's own
  architecture, Spec Kit artifacts, and Environment Contract, never inferred
  from the runner's implementation language.
- "PascalCaseBlockName" in a branch name is derived from the orchestrator's
  chosen block name (e.g. a block named "Validation Pipeline" yields
  `ValidationPipeline`); the exact derivation rule (casing/character
  stripping edge cases) is left to planning if not already unambiguous.
- A human-readable configuration format (e.g. YAML, TOML, or Markdown with
  structured front matter) is assumed sufficient for the project workflow
  configuration; the exact format is chosen during planning, informed by
  the runner's Python + `uv` implementation.
- Existing Spec Kit tooling (specify CLI, `.specify/` scripts and templates)
  is reused as-is for specification/plan/task lifecycle; this workflow does
  not reimplement Spec Kit itself, only orchestrates around it.
- The legacy global skill `~/.claude/skills/solari-speckit-executor/
  SKILL.md` is treated as reference/legacy only; v1 does not depend on its
  presence on any machine.
- Release automation for this workflow repository (e.g. automated version
  bumps/publishing) is out of scope for v1; semantic version numbers are
  recorded manually for now.
- This specification defines required behavior and constraints. The runner's
  implementation language and environment manager (Python, `uv`) are decided
  per FR-038; the specific supported Python version, file format library, or
  locking primitive remain technical-plan decisions, made consistent with
  the constitution's Minimal, Justified Dependencies principle.
