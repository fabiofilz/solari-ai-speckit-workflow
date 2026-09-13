# Implementation Plan: AI Spec Kit Workflow v1

**Branch**: `001-workflow-v1` | **Date**: 2026-09-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-workflow-v1/spec.md`

**Note**: This plan covers technical planning only. Per the spec's own scope
clarification, **v1 does not implement the runner, Codex integration,
`.ai-runs/` automation, or Git/checkpoint automation** — this plan defines
HOW those will be built so `/speckit.tasks` can decompose them into
implementation tasks. No runner code is produced by this plan.

## Summary

This plan is built around six distinct components, not five — a point the
prior draft blurred and this pass corrects everywhere: **the User** (a
human) requests and authorizes work; **ChatGPT/the Orchestrator**
interprets that request and makes every engineering decision; **the
Python Runner** executes and verifies; **Claude Code** implements; **Codex**
validates; **Spec Kit** defines the formal contract they all work against
(research.md §0.0). The User and the Orchestrator are never the same actor
in this design, and the User is never required to manually coordinate
internal mechanics (branch creation, invoking Claude/Codex, checkpointing)
— those are the Orchestrator's and Runner's job once the User authorizes a
number of blocks.

The Runner is a **Python 3.12 CLI tool, managed end-to-end with `uv`**,
that is the **sole, exclusive interface between this workflow and Git**
(research.md §0.1/§0.4) — repository inspection, branch creation, Candidate
Tree construction, checkpoint staging, the checkpoint commit,
`merge --no-ff`, tag creation, and branch retention all happen only in
Runner code. **Claude Code and Codex are absolutely prohibited from
executing any Git command** — no `status`, `diff`, `add`, `commit`,
`checkout`, `branch`, `merge`, `tag`, `push`, or anything else (research.md
§0.11/§0.12); Claude implements by editing files in a working tree the
Runner already prepared, and Codex reviews the state the Runner exposes to
it, using Runner-supplied diff context if a review needs one. Neither ever
decides block boundaries, either. The Runner technically enforces the
invariants fixed by the constitution and spec: strict sequential
single-run ownership of a project's working tree, a stack-conditional
Project Readiness Gate, and a **Candidate Tree** fingerprint — built from
an isolated temporary Git index, never the real one — that proves a
checkpoint matches the exact uncommitted state Codex reviewed (research.md
§12, correcting an earlier draft's assumption that Codex reviews an
already-committed tree, and this pass's further correction that Claude
itself never commits at all — implementation stays uncommitted end to end
until the Runner's own checkpoint commit). Push stays **manual-only** for
v1 via a small, forward-compatible `[git.push] mode = "manual"` policy,
and requires the **User's** explicit authorization even in the CLI-exposed
future case (research.md §0.2) — Claude and Codex never push under any
circumstances. How many blocks a run covers (`requested_block_count`)
originates with the **User** and is interpreted by the **Orchestrator**;
the Runner's CLI has no notion of it at all — every subcommand acts on
exactly one already-identified block and stops (research.md §0.3), and an
interruption consumes the entire current multi-block authorization rather
than leaving a remaining count to resume into automatically. An
interrupted block resumes from its last safely-completed stage — only
after the User authorizes the resume — via a small persistent
`.ai-runs/.block-state.json` and a seven-value state vocabulary, with at
most one automatic retry for genuinely transient failures and an immediate
stop for everything else (research.md §0.6/§0.7). The Runner ships with
**zero third-party runtime dependencies** — stdlib `tomllib` reads a new
`.ai-workflow.toml` project configuration (replacing the YAML placeholder
from the spec's assumptions once `tomllib`'s built-in, zero-dependency
read support made TOML the better fit), stdlib `subprocess`/`os.open`
handle cross-platform process execution and single-run locking, and
stdlib `hashlib`/`git` plumbing produce the Candidate Tree fingerprint.
`pytest` is the one dependency, and it is dev-only. Every stage
(`start-block`, `run-claude`, `run-codex-gate`, `checkpoint`, `resume`,
`retention`) is a separate CLI subcommand, issued only by the Orchestrator
— there is no "run everything" command and no "run N blocks" command —
which is itself the technical enforcement of Controlled Automation
(Principle VII).

## Technical Context

**Language/Version**: Python 3.12 (exact patch pinned via `runner/.python-version`,
provisioned automatically by `uv` — no OS-managed Python assumed; see
[research.md](./research.md) §1)

**Primary Dependencies**: None at runtime (stdlib only: `tomllib`,
`subprocess`, `hashlib`, `argparse`, `pathlib`, `dataclasses`, `os`,
`shutil`, `shlex`, `unicodedata`, `datetime`, `json`). `pytest` is a
dev-only dependency for the runner's own test suite. See
[research.md](./research.md) §3.

**Storage**: N/A — state lives in Git objects/refs/tags and flat Markdown/
JSON files under the consuming project's `.ai-runs/`; no database.

**Testing**: `pytest`, run via `uv run pytest` from `runner/`. Real `git`
CLI against temporary repos; fake/stub `claude`/`codex` CLIs for actor
integration tests. See [research.md](./research.md) §18.

**Target Platform**: macOS, Windows, and Linux (where practical), as a CLI
tool invoked by a human/orchestrator or scripted from ChatGPT's side.

**Project Type**: Single-project CLI / developer tool, living at `runner/`
inside this repository (not a web service, not a library consumed by other
Python code).

**Performance Goals**: No hard targets. Runner-side overhead (readiness
checks, fingerprint computation, Git plumbing) is expected to complete in
low single-digit seconds; end-to-end block latency is dominated by the
invoked `claude`/`codex` CLI sessions, not the runner.

**Constraints**: No `shell=True` anywhere; no network calls originate from
the runner itself (only the external CLIs it invokes may reach the
network); must not assume a pre-existing OS-managed Python or a specific
shell.

**Scale/Scope**: One project working tree owned by at most one execution at
a time, by design (Sequential Execution is an invariant, not a scale
limit). `.ai-runs/` grows unboundedly but as flat files — no scale concern
for v1.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Status | How this plan satisfies it |
|---|---|---|
| I. Explicit Actor Separation | PASS | User and Orchestrator are modeled as distinct actors throughout (research.md §0.0) — the Orchestrator never inherits the User's authorization implicitly. Session mode (NEW/CONTINUE) is always an explicit CLI argument the Orchestrator supplies — the runner never infers or defaults to CONTINUE (research.md §9). The run lock records tool/model/effort/session-mode/purpose per execution (research.md §7). Claude Code and Codex are absolutely prohibited from executing **any** Git command, not merely checkpoint-level ones (research.md §0.11/§0.12) — the CLI never exposes a Git-touching capability to either. Codex's gate is technically prevented from silently passing off writes as reads: the runner rebuilds the Candidate Tree immediately before and after the gate and hard-fails if its OID changed (research.md §10, §12). |
| II. Specification Before Implementation | PASS | The Readiness Gate's common checks include "required Spec Kit artifacts exist" (research.md §11); branch/tag naming is derived from Spec Kit task IDs (research.md §13, §16), and `start-block` is the only path that creates a branch, always from an orchestrator-supplied task range (research.md §0.10). |
| III. Independent Review Before Checkpoint | PASS | Checkpoint flow (research.md §13) hard-requires the latest gate record for the active branch to be PASS with zero unresolved blocking-severity findings before staging is even attempted; QA remediation always stays on the same branch and never auto-loops without an orchestrator decision in between (research.md §0.5). |
| IV. Sequential Execution | PASS | Single-run ownership via an atomic, `O_EXCL`-created lock file — no third-party locking library needed, works identically on macOS/Windows/Linux (research.md §7). Persistent block-state (research.md §0.7) complements this: the lock covers one process's duration, block-state covers one block's duration across several invocations. |
| V. Reproducible Environments | PASS | `.ai-workflow.toml`'s `[environment]` table is the Environment Contract; readiness checks validate it per-stack, skipping inapplicable sections rather than failing them (research.md §4, §11). |
| VI. Auditable Checkpoints | PASS | Candidate Tree fingerprint (research.md §12) gates every checkpoint via a verified invariant chain (Candidate Tree → Fingerprint → staged tree → commit tree, research.md §13); `git merge --no-ff` + annotated tag with required metadata (research.md §13, §14); push stays manual-only (`[git.push] mode = "manual"`, research.md §0.2). |
| VII. Controlled Automation | PASS | The CLI is a set of discrete subcommands (`start-block`, `run-claude`, `run-codex-gate`, `checkpoint`, `resume`, `retention`) with no orchestrating "do everything" or "do N blocks" command — the absence of those commands is the technical enforcement, not a documentation promise (research.md §0.3/§0.4). Automatic retry is deliberately narrow (one retry, transient failures only) so it never substitutes for a required orchestrator decision point (research.md §0.6). |
| VIII. Cross-Project Portability | PASS | `.ai-workflow.toml` schema (research.md §4, data-model.md) carries no project-specific defaults; readiness checks are stack-conditional via a small internal registry, not hard-coded to one stack (research.md §11). |
| IX. Minimal, Justified Dependencies | PASS (exceeds bar) | Zero third-party runtime dependencies; every stdlib choice is justified against a concrete alternative in research.md §3, including the Candidate Tree mechanism, which uses only `git` plumbing and stdlib `subprocess`. `pytest` is dev-only and does not ship to end users. |

No violations. Complexity Tracking is left empty.

## Project Structure

### Documentation (this feature)

```text
specs/001-workflow-v1/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md         # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/            # Phase 1 output
│   ├── cli-interface.md
│   ├── config-schema.md
│   └── codex-gate-result-contract.md
└── tasks.md              # Phase 2 output (/speckit-tasks — not created here)
```

### Source Code (repository root)

The runner lives under `runner/` per
[`docs/architecture/repository-structure.md`](../../docs/architecture/repository-structure.md),
as a single `src/`-layout Python project (Option 1: single project — this is
a standalone CLI tool, not a web app or mobile+API split, so Options 2/3 do
not apply and are omitted).

```text
runner/
├── pyproject.toml                 # uv-managed project; requires-python, [project.scripts]
├── .python-version                # exact pinned patch (e.g. 3.12.8), uv-provisioned
├── uv.lock                        # committed, reproducible dependency lock
├── src/
│   └── solari_workflow/
│       ├── __init__.py
│       ├── __main__.py            # `python -m solari_workflow`
│       ├── cli.py                 # argparse subcommand entry point
│       ├── errors.py              # shared exception types
│       ├── config/
│       │   ├── schema.py          # dataclasses for Project Workflow Config
│       │   ├── loader.py          # tomllib-based load + validation
│       │   └── errors.py
│       ├── speckit/
│       │   └── integration.py     # locates/reads Spec Kit artifacts; never reimplements Spec Kit
│       ├── git/
│       │   ├── ops.py             # thin subprocess wrappers over `git`
│       │   ├── branch.py          # PascalCase derivation, branch-name parsing
│       │   ├── candidate_tree.py  # isolated temp-index Candidate Tree build (research.md §12)
│       │   ├── checkpoint.py      # stage/commit-tree/update-ref/merge --no-ff/tag (research.md §13)
│       │   └── retention.py       # branch retention algorithm
│       ├── actors/
│       │   ├── claude.py          # Claude Code CLI integration + STATUS contract parser
│       │   └── codex.py           # Codex CLI integration + RESULT contract parser
│       ├── readiness/
│       │   ├── engine.py          # core engine, stack-conditional dispatch
│       │   └── checks/
│       │       ├── common.py      # config valid, Spec Kit present, .ai-runs ignored, etc.
│       │       ├── python_stack.py
│       │       ├── node_stack.py
│       │       ├── go_stack.py
│       │       ├── rust_stack.py
│       │       ├── docker.py
│       │       └── local_model.py
│       ├── fingerprint/
│       │   └── engine.py          # Fingerprint record (head_oid, candidate_tree_oid, ...); compute/verify (research.md §12)
│       ├── state/
│       │   └── block_state.py     # .ai-runs/.block-state.json: state vocabulary, retry count, resume (research.md §0.7)
│       ├── runs/
│       │   └── audit.py           # .ai-runs/ prompt/result lifecycle
│       ├── lock/
│       │   └── run_lock.py        # O_EXCL single-run ownership lock
│       └── platform/
│           └── proc.py            # cross-platform subprocess + PID-liveness + single-retry helpers
└── tests/
    ├── unit/
    ├── integration/                # real temp git repos, fake claude/codex CLIs
    └── fixtures/
        ├── fake_claude.py          # emits controllable STATUS:/exit-code behavior, incl. simulated transient failure
        ├── fake_codex.py           # emits controllable RESULT:/findings behavior
        └── projects/                # minimal python/node/go/rust fixture trees
```

**Structure Decision**: Single-project `src/` layout under `runner/`,
isolated from `schema/`/`templates/` (which stay consumable without the
runner existing) and from any consuming project's own source tree. This
mirrors `docs/architecture/repository-structure.md`'s `runner/checks/` and
`runner/checkpoint/` separation, renamed to `readiness/` and `git/
checkpoint.py` respectively to match the vocabulary used throughout the
spec and constitution. `git/candidate_tree.py` and `state/block_state.py`
are new modules added by this correction pass (research.md §12, §0.7).

## Complexity Tracking

*No Constitution Check violations were identified — this table is
intentionally empty.*
