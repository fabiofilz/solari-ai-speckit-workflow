# Proposed Repository Structure (v1, architecture-level)

This describes the intended shape of this repository once the runner is
implemented. It is a proposal to be confirmed/adjusted during technical
planning (`/speckit.plan`) — nothing under `runner/` exists yet. The
runner's implementation language (Python, managed with `uv`) is a decided
v1 architectural decision; only its internal layout details remain open
for planning.

```
solari-ai-speckit-workflow/
├── .specify/                        # Spec Kit scaffolding (already initialized)
│   ├── memory/constitution.md       # durable principles (this repo's own)
│   ├── templates/                   # spec/plan/tasks/checklist templates
│   └── scripts/bash/                # Spec Kit lifecycle scripts
├── .claude/skills/                  # speckit-* skills (already initialized)
├── specs/
│   └── 001-workflow-v1/
│       ├── spec.md                  # this repository's own v1 specification
│       ├── plan.md                  # (future) technical plan
│       └── tasks.md                 # (future) task breakdown
├── docs/
│   └── architecture/
│       └── repository-structure.md  # this file
├── schema/                          # (future) project workflow config schema
│   ├── project-workflow-config.*    # format decided in planning (YAML/TOML/etc.)
│   └── environment-contract.*       # Environment Contract schema/sub-schema
├── templates/                       # (future) bootstrap templates for consuming projects
│   ├── project-workflow-config.example.*
│   └── environment-contract.example.*
├── runner/                          # (future) the orchestration runner — NOT YET IMPLEMENTED
│   ├── pyproject.toml               # uv-managed Python project (exact layout: planning)
│   ├── src/                         # implementation (Python, managed with uv)
│   ├── checks/                      # Project Readiness Gate checks (stack-conditional)
│   ├── checkpoint/                  # fingerprint, merge --no-ff, tag creation, retention
│   └── tests/                       # runner's own test suite
├── docs/
│   └── usage/                       # (future) worked examples, bootstrap walkthroughs
├── README.md
└── LICENSE
```

## Placement rationale

- **`schema/` and `templates/` are separated from `runner/`** so a consuming
  project (or a human) can read/generate a project workflow configuration
  without needing the runner implementation to exist yet — the schema is the
  contract; the runner is one (future) implementation that enforces it.
- **`runner/checks/` and `runner/checkpoint/` are separated modules**,
  mirroring the constitution's principles (Reproducible Environments →
  checks; Auditable Checkpoints → checkpoint) so each can be tested and
  evolved independently, and so a future alternate implementation of either
  half remains plausible without a rewrite of the other.
- **No project-specific directories anywhere in this tree.** Any
  project-specific data (including anything from `solari-ai-pdf-document-
  converter`) belongs only inside a *consuming* project's own `.ai-runs/`
  and workflow-config file — never inside this repository.
- **`docs/usage/` is kept separate from the top-level `README.md`** so the
  README stays a concise architectural overview while worked examples and
  longer walkthroughs can grow independently.

## What is intentionally not yet decided here

- The concrete file format/extension for `schema/` and `templates/` (e.g.
  `.yaml` vs `.toml` vs structured Markdown) — deferred to planning.
- The runner's implementation language and manager are decided (Python,
  managed with `uv`); the exact supported Python version, package/project
  layout, and directory conventions beneath `runner/src/` are deferred to
  technical planning (FR-038). This decision applies only to the runner —
  it does not constrain the language/stack of any consuming project.
- Whether the Project Readiness Gate checks are one executable or a
  plugin-per-stack set of scripts — deferred to planning.
- Release/versioning automation for this repository — explicitly out of
  scope for v1 per the specification's assumptions.
