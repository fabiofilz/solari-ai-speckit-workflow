---

description: "Task list for AI Spec Kit Workflow v1 Runner implementation"
---

# Tasks: AI Spec Kit Workflow v1 Runner

**Input**: Design documents from `/specs/001-workflow-v1/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all finalized)

**Tests**: Included. The constitution's Specification Before Implementation /
Auditable Checkpoints principles and this feature's own explicit testing
strategy require behavioral contracts and critical invariants to be
testable before or alongside their implementation — test tasks are marked
`[P]` where they exercise a fixture/module in isolation from the same-phase
implementation task, and are otherwise sequenced immediately after the unit
they cover so failures are caught at the smallest reproducible scope.

**Organization**: Tasks are grouped by user story (spec.md) to enable
independent implementation and testing of each story, after a Foundational
phase that every story depends on.

**IMPORTANT — execution boundary**: This document decomposes implementation
work. It does **not** authorize implementation and does **not** authorize any
Git operation. No task here may be started under `/speckit.implement` or any
other process without a separate, explicit Orchestrator/User go-ahead per
task block. Runner code that itself invokes `git` (git/ops.py, candidate_tree.py,
checkpoint.py, retention.py) is implementation-under-test, not Claude or Codex
performing Git operations on this repository — per the canonical responsibility
model, Claude Code and Codex never execute Git commands, during this task
generation or during any future implementation of these tasks.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on an
  incomplete task in the same phase)
- **[Story]**: Which user story this task belongs to (US1–US5); Setup,
  Foundational, and Polish tasks carry no story label
- File paths are relative to the repository root; the runner lives under
  `runner/` per plan.md's Project Structure

## Path Conventions

Single-project CLI tool at `runner/src/solari_workflow/` (package code) and
`runner/tests/` (unit/integration/fixtures), per plan.md.

---

## Phase 1: Setup

**Purpose**: Bring up the `runner/` project shell so every later task has a
place to live and a test runner to run against.

- [ ] T001 Create the `runner/` project skeleton: `src/solari_workflow/{config,speckit,git,actors,readiness/checks,fingerprint,state,runs,lock,platform}/` packages (each with `__init__.py`) and `tests/{unit,integration,fixtures/projects}/` directories, matching plan.md's Project Structure exactly
- [ ] T002 Initialize the uv-managed Python 3.12 project: `runner/pyproject.toml` (`[project]` metadata, `requires-python = ">=3.12,<3.13"`, `[project.scripts] solari-workflow = "solari_workflow.cli:main"`, `[dependency-groups] dev = ["pytest"]`), `runner/.python-version` pinned to an exact 3.12.x patch, and a committed `runner/uv.lock` generated via `uv lock`
- [ ] T003 [P] Configure `pytest` (`[tool.pytest.ini_options]` in `runner/pyproject.toml`: test paths, discovery patterns) and add one trivial smoke test in `runner/tests/unit/test_smoke.py`; verify `uv run pytest` executes it successfully from `runner/`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The primitives every user story is built on — config loading,
Git plumbing, locking, the audit trail, common readiness checks, and the CLI
skeleton itself. No user-story task may begin before this phase is complete.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [ ] T004 Implement shared exception types in `runner/src/solari_workflow/errors.py`: config-validation errors, Git-ambiguity errors, staging-precondition-violation errors, unsupported-CLI-capability errors, and their mapping to the CLI's fixed exit-code convention (`0` success, `1` substantive-negative-result, `2` stopped-for-manual-intervention) per `contracts/cli-interface.md`
- [ ] T005 [P] Implement `runner/src/solari_workflow/platform/proc.py`: list-argv-only subprocess wrapper (never `shell=True`), `shutil.which` executable resolution with an explicit "not found on PATH" error, `ProcessResult(returncode, stdout, stderr, duration)` decoded UTF-8 with `errors="replace"`, a `CommandFailed` exception for runner-internal failures, a streaming `Popen`-based helper (line-by-line background-thread read, simultaneously echoed and returned for transcript capture) for long-running `claude`/`codex` sessions, a PID-liveness check usable on POSIX and Windows, and the single-retry-for-transient-failures helper shared by every actor/Git invocation (research.md §0.6/§6)
- [ ] T006 [P] Unit tests for `platform/proc.py` in `runner/tests/unit/test_proc.py`: missing-executable error shape, non-zero exit surfaced as data vs. raised `CommandFailed`, streaming capture correctness, no shell metacharacter interpretation from argv content, and the single-retry helper firing exactly once before giving up (depends on: T005)
- [ ] T007 [P] Implement config dataclasses in `runner/src/solari_workflow/config/schema.py` mirroring data-model.md's "Project Workflow Configuration" and "Environment Contract" field tables (required/optional/stack-conditional markers preserved as structure, not just comments)
- [ ] T008 Implement `runner/src/solari_workflow/config/loader.py`: `tomllib`-based parse (surfacing `TOMLDecodeError` verbatim with file path and line/column), `[workflow].schema_version` gate (only `"1"` accepted, else a hard failure naming supported versions), aggregated schema validation (every problem reported together, each naming its exact TOML path), unknown-key/table leniency (warning only) vs. missing/malformed-required-field hard failure, and fixed-enum hard validation for `git.push.mode` (`"manual"` only) and `git.checkpoint_merge_strategy` (`"no-ff"` only) (depends on: T007)
- [ ] T009 [P] Unit tests for the config loader in `runner/tests/unit/test_config_loader.py`: a fully valid fixture; missing required field(s) reported together in one aggregated failure; unrecognized `schema_version`; an unknown key/table producing a warning, not a failure; `git.push.mode = "auto"` failing hard as an invalid enum value (not covered by unknown-key leniency); stack-conditional fields (e.g. `environment.lockfile`) correctly treated as absent-is-not-an-error at the loader level (depends on: T008)
- [ ] T010 Implement `runner/src/solari_workflow/git/ops.py`: thin `git` subprocess wrappers over `platform/proc.py` for `rev-parse`, `diff --cached --quiet` / `--name-status` (the reusable real-staging-area precondition function, research.md §0.8, called from `start-block`/`run-codex-gate`/`checkpoint`), `branch --merged`, `merge-base --is-ancestor`, `switch`, `merge --no-ff`, `tag -a`, `update-ref`, `commit-tree`, `write-tree`, `read-tree`, `checkout-index`, and `rev-parse --verify <commit-ish>^{tree}` (tree-OID retrieval/comparison) — no `shell=True` anywhere (depends on: T005)
- [ ] T011 [P] Implement `runner/src/solari_workflow/git/branch.py`: PascalCase block-name derivation (NFKD normalize + combining-mark strip, `[^A-Za-z0-9]+` word-split, acronym/numeric-word casing rules, `Block{first_task_id}` fallback for non-representable input) and branch-name/tag-name build + regex-parse helpers (`T{first}-{PascalCase}`, `checkpoint-T{first}-T{last}`), per research.md §16 — the single normalization routine also reused by `.ai-runs` slug derivation
- [ ] T012 [P] Unit tests for PascalCase/branch-naming in `runner/tests/unit/test_branch_naming.py`: a table covering spaces, hyphens, punctuation, acronyms (`"API"`), numeric words, accented Latin diacritics, and non-Latin-script fallback to `Block{id}`; branch/tag name build-then-parse round-trips (depends on: T011)
- [ ] T013 Implement `runner/src/solari_workflow/speckit/integration.py`: read-only Spec Kit artifact discovery (specification/clarification/plan/tasks presence per configured `speckit.spec_dir`) and a `tasks.md` task-ID-range → title lookup used by checkpoint tag metadata — never reimplements Spec Kit's own CLI/scripts (depends on: T007)
- [ ] T014 [P] Unit tests for `speckit/integration.py` in `runner/tests/unit/test_speckit_integration.py` against fixture Spec Kit trees: artifacts fully present, partially present, and entirely absent; task ID/title lookup for a given range, including an ID outside the file's range (depends on: T013)
- [ ] T015 Implement `runner/src/solari_workflow/lock/run_lock.py`: atomic `O_EXCL`-created `.ai-runs/.lock`, JSON payload (`pid, hostname, tool, model, effort, session_mode, scope, purpose, started_at, lock_schema_version`), refusal on `FileExistsError` with an actionable message naming the current owner, best-effort non-destructive stale-liveness annotation (`os.kill(pid, 0)` on POSIX; a Windows-appropriate equivalent), and guaranteed release in a `finally` block — the runner never auto-removes another process's lock (research.md §7)
- [ ] T016 Implement `runner/src/solari_workflow/runs/audit.py`: `.ai-runs/YYYYMMDD-NNN-slug-{prompt,result}.md` sequence numbering (max-used `NNN` for the day, then `+1`), atomic `O_EXCL` file creation with a bounded collision-retry (never overwrite, research.md FR-024/SC-007), slug derivation reusing T011's normalization routine, plain-Markdown prompt/result metadata headers (tool, model, effort, session mode + prior-session-id when `CONTINUE`, Spec Kit involvement, scope, purpose, UTC timestamp, workflow schema/version), and immutability (never reopens an existing pair for editing)
- [ ] T017 [P] Unit tests for `runs/audit.py` in `runner/tests/unit/test_audit.py`: sequence numbering across multiple same-day files, a forced naming collision resolved by retry without ever overwriting the existing file, slug derivation matches the shared normalization routine, and an attempt to "edit" an existing pair is rejected/impossible via the module's own API (depends on: T016)
- [ ] T018 Implement `runner/src/solari_workflow/readiness/engine.py`: the core engine plus **common checks only** (config valid, `.ai-runs/` excluded from Git, main branch identified, checkpoint strategy defined, a heuristic secrets scan over config string values) — the stack-specific registry dispatch point exists but is populated later (Phase 6); returns the full `CheckResult` list including any `SKIPPED` entries (depends on: T008, T010)
- [ ] T019 [P] Unit tests for readiness common checks in `runner/tests/unit/test_readiness_common.py`: each common check's PASS/FAIL, a suspicious config value (e.g. `sk-...`-shaped string) flagged by the secrets heuristic, and `.ai-runs/` tracked-by-Git correctly failing (depends on: T018)
- [ ] T020 Implement CLI foundation: `runner/src/solari_workflow/cli.py` (argparse root parser + subparsers for `init`, `readiness-check`, `start-block`, `run-claude`, `run-codex-gate`, `checkpoint`, `resume`, `retention`, `status` — bodies delegate to the modules implemented as they become available, `NotImplementedError` placeholders for the rest) and `runner/src/solari_workflow/__main__.py`, with the uniform exit-code convention and stderr error formatting from `errors.py` applied at the CLI boundary (depends on: T004, T005, T008, T015)
- [ ] T021 [P] Integration test in `runner/tests/integration/test_cli_skeleton.py`: every declared subcommand responds to `--help` successfully; an unknown subcommand or flag fails with a clear usage error and non-zero exit (depends on: T020)

**Checkpoint**: Config loading, Git plumbing, locking, the audit trail, common
readiness checks, and the CLI skeleton all exist and are independently
tested — every user story below can now be implemented.

---

## Phase 3: User Story 1 - Bootstrap the workflow onto a new project (Priority: P1) 🎯 MVP

**Goal**: `solari-workflow init` scaffolds a valid, generic `.ai-workflow.toml`
for any stack, with zero hard-coded reference-project values, and ensures
`.ai-runs/` plus the lock/block-state paths are Git-ignored; bootstrap
documentation covers the `uv`/Python/GitHub-auth prerequisites end-to-end.

**Independent Test**: Apply the documented bootstrap steps to a throwaway
project of a different technology stack (e.g. Go) and confirm the resulting
config contains no PDF-Converter-specific values and no hard failures on
inapplicable sections.

- [ ] T022 [P] [US1] Integration test in `runner/tests/integration/test_init_command.py`: `solari-workflow init --project-name X --stack go` against an empty temp repo produces a schema-valid `.ai-workflow.toml` (`environment.stack = "go"`, `[git.push] mode = "manual"`) with no reference-project values anywhere; a rerun without `--force` refuses (exit `1`); a rerun with `--force` shows a diff before writing (spec.md SC-001, quickstart Scenario 1)
- [ ] T023 [US1] Implement `solari-workflow init` in `runner/src/solari_workflow/cli.py` (+ a dedicated `config/init.py` template/writer module): accepts orchestrator-supplied values per stack, writes `.ai-workflow.toml` from a generic template (never guesses a project-specific value), refuses to overwrite an existing config without `--force` (diff-before-write), and ensures `.ai-runs/` plus lock/block-state paths are added to `.gitignore` (depends on: T008, T020)
- [ ] T024 [US1] Write bootstrap/installation documentation (`docs/bootstrap.md` or equivalent per `docs/architecture/repository-structure.md`'s conventions): `uv` install prerequisite (no OS-managed Python assumed), `uv tool install --from git+...` runner install, the private-repo GitHub authentication step, `solari-workflow init` usage, and a pointer to Spec Kit's own `specify init` / `/speckit.*` as a separate, reused step (FR-013/FR-037)
- [ ] T025 [US1] Execute quickstart.md Scenario 1 (bootstrap a throwaway Go-stack project) end-to-end against the completed `init` command and record the outcome in the bootstrap documentation (depends on: T023, T024)

**Checkpoint**: A throwaway project of a different stack can be bootstrapped
onto this workflow using only the documented steps, independent of any
block-execution machinery.

---

## Phase 4: User Story 2 - Run one block through the Readiness Gate, implementation, independent review, and checkpoint (Priority: P1)

**Goal**: The full block lifecycle — `start-block` creates the branch,
`run-claude` implements without ever touching Git, `run-codex-gate` builds
the Candidate Tree/Fingerprint and independently validates, and `checkpoint`
re-verifies the invariant chain before performing the runner-owned merge and
tag.

**Independent Test**: Run one small, self-contained task block end-to-end
(fake `claude`/`codex` stubs) and confirm a branch named
`T{first}-{PascalCaseBlockName}` was used, Codex's gate ran only after
Claude's session ended, a `checkpoint-T{first}-T{last}` tag exists with the
required metadata, and the working tree matches the fingerprint recorded at
gate time.

**Note on cross-story dependency**: `start-block`'s readiness call uses
Phase 2's common-checks-only engine for this story's own independent test
(a stack-agnostic fixture project); Phase 6 (US4) later populates the
stack-specific registry that `start-block` calls through the same interface.

- [ ] T026 [P] [US2] Test fixtures `runner/tests/fixtures/fake_claude.py` (controllable trailing `STATUS: COMPLETE|ARCHITECTURAL_DECISION_REQUIRED|BLOCKED` marker + exit code, including a simulated transient launch failure) and `runner/tests/fixtures/fake_codex.py` (controllable trailing `RESULT: PASS|FAIL` / `FINDINGS:` block + exit code), invoked via `sys.executable <script>` with no shell dependency, per research.md §18
- [ ] T027 [US2] Implement `runner/src/solari_workflow/git/candidate_tree.py`: isolated temporary-index Candidate Tree construction (`GIT_INDEX_FILE`-redirected `read-tree HEAD` + `add -A` + `write-tree`, temp index path outside the working tree, always-cleanup in a `finally`) that never reads or writes the real `.git/index` (depends on: T010)
- [ ] T028 [P] [US2] Unit tests for Candidate Tree construction in `runner/tests/unit/test_candidate_tree.py`: deterministic OID for fixed inputs; changes on tracked-file content change, deletion, a new untracked-and-not-ignored file, a file-mode change, and (where the platform supports it) a symlink change; unaffected by changes to Git-ignored paths (including `.ai-runs/`); the real index's own `write-tree` OID is asserted unchanged before and after the build (depends on: T027)
- [ ] T029 [US2] Implement `runner/src/solari_workflow/fingerprint/engine.py`: the Fingerprint record (`head_oid, candidate_tree_oid, branch_name, first_task, last_task, computed_at`), a compute function, a strict-equality verify function (both OIDs, never one alone), and a short derived label (`sha256(f"{head_oid}:{candidate_tree_oid}")[:16]`) for filenames/logs only (depends on: T027)
- [ ] T030 [P] [US2] Unit tests for the fingerprint engine in `runner/tests/unit/test_fingerprint.py`: equality requires both `head_oid` and `candidate_tree_oid` to match; a mismatch in either alone is detected; the derived label is stable for identical inputs (depends on: T029)
- [ ] T031 [US2] Implement `runner/src/solari_workflow/state/block_state.py`: the `.ai-runs/.block-state.json` schema (branch/task-range/block-name identity, the 7-value `state` vocabulary, the `last_safe_stage` vocabulary, `retry_count_current_stage` fixed at a `0`/`1` ceiling, `last_gate`, `updated_at`), read/write helpers, and a regeneration path that reconstructs enough state from `.ai-runs/*-result.md` history plus current Git state when the file is missing or looks stale (depends on: T016)
- [ ] T032 [P] [US2] Unit tests for `state/block_state.py` in `runner/tests/unit/test_block_state.py`: valid state-vocabulary transitions, the fixed single-retry ceiling never exceeding `1`, and regeneration from `.ai-runs/*-result.md` history producing an equivalent state when the JSON file is deleted (depends on: T031)
- [ ] T033 [US2] Implement `runner/src/solari_workflow/actors/claude.py`: invoke the `claude` CLI (`shutil.which`) via `platform/proc.py`, requiring explicit orchestrator-supplied model/effort/session-mode arguments (no default inference — `actors/claude.py` itself refuses to run without them), an isolated `_build_claude_argv(...)` flag-mapping function, fail-closed behavior with an actionable error when the installed CLI lacks a requested capability, trailing `STATUS:` marker parsing (missing/unparseable → `BLOCKED`), and streamed transcript persistence into the run's `.ai-runs/*-result.md` (depends on: T005, T016)
- [ ] T034 [P] [US2] Unit tests for `actors/claude.py` in `runner/tests/unit/test_actor_claude.py` using the fake Claude stub: all three `STATUS:` values parsed correctly, and a missing/malformed marker treated as `BLOCKED` (depends on: T026, T033)
- [ ] T035 [US2] Implement `runner/src/solari_workflow/actors/codex.py`: always starts a NEW Codex session (no `session_mode` concept at all), parses the trailing `RESULT: PASS|FAIL` / `FINDINGS:` block exactly per `contracts/codex-gate-result-contract.md` (fail-closed to `FAIL` on anything missing/malformed/case-mismatched), and does not itself decide checkpoint eligibility — that stays the caller's job against `[checkpoint].blocking_severities` (depends on: T005, T016)
- [ ] T036 [P] [US2] Unit tests for `actors/codex.py` in `runner/tests/unit/test_actor_codex.py`: well-formed `PASS`/`FAIL`, a missing trailing block, a finding with an invalid/missing `severity`, case-sensitivity (`"Pass"` is unparseable), and an empty `FINDINGS:` list accepted as "no findings" (depends on: T026, T035)
- [ ] T037 [US2] Implement `solari-workflow start-block`: run the (Phase 2) readiness gate → real staging-area precondition (`git/ops.py`) → verify no branch matching the derived name already exists unexpectedly → create + check out `T{first}-{PascalCase(block_name)}` from current `main` (`git/branch.py`) → initialize block-state (`state: RUNNING`, `last_safe_stage: BRANCH_CREATED`) (depends on: T011, T018, T031)
- [ ] T038 [US2] Implement `solari-workflow run-claude`: verify the current branch matches the active block-state's `branch_name` (refuse rather than create one if not) → invoke `actors/claude.py` → update block-state per the parsed `STATUS:` outcome → apply the shared single-retry-then-`BLOCKED_MANUAL` policy for operational launch failures (depends on: T033, T031)
- [ ] T039 [US2] Implement `solari-workflow run-codex-gate`: branch check → staging precondition (fail fast before spending a Codex session) → build Candidate Tree + Fingerprint X → prepare optional Runner-generated diff context via `git/ops.py` → invoke `actors/codex.py` → immediately rebuild the Candidate Tree and hard-fail (`Codex gate modified the working tree`) on any drift, regardless of Codex's own reported result → parse the structured result → record Fingerprint X + result into `.ai-runs/*-result.md` and block-state (`last_safe_stage: GATE_PASSED` only on an eligible PASS) (depends on: T027, T029, T035, T031)
- [ ] T040 [US2] Implement `runner/src/solari_workflow/git/checkpoint.py` + `solari-workflow checkpoint`: branch verification → staging precondition → gate-PASS verification against `[checkpoint].blocking_severities` → Fingerprint recheck (exact `CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE` message on any mismatch, stop without touching the index) → stage the Candidate Tree into the real index (`read-tree` + `checkout-index -a -f` if needed) → verify the staged tree equals `candidate_tree_oid` → `commit-tree` + compare-and-swap `update-ref` → verify the commit's tree equals `candidate_tree_oid` → switch to `main` and confirm no unexpected divergence → `merge --no-ff` → verify the merge (hard stop, no auto-resolution, on conflict) → create the annotated `checkpoint-T{first}-T{last}` tag with required metadata (block name, branch name, task range with titles via `speckit/integration.py`, `Gate: PASS`, `Checkpoint: VERIFIED`) plus any already-available optional metadata → set block-state `state: COMPLETED` (depends on: T010, T029, T031, T013)
- [ ] T041 [P] [US2] Integration test in `runner/tests/integration/test_block_lifecycle_happy_path.py` (real temp Git repo + fake stubs): the full chain `start-block → run-claude → run-codex-gate → checkpoint`; asserts the branch name pattern, the `checkpoint-T{first}-T{last}` tag with every required metadata field, and that the checkpoint commit's tree equals the gated `candidate_tree_oid` (depends on: T037, T038, T039, T040)
- [ ] T042 [P] [US2] Integration test in `runner/tests/integration/test_qa_remediation_cycle.py`: a Codex `FAIL` with a blocking-severity finding → a simulated orchestrator-initiated `run-claude` remediation on the **same branch** (no new branch created) → a fresh `run-codex-gate` (new Fingerprint) → `PASS` → `checkpoint` succeeds (depends on: T041)
- [ ] T043 [P] [US2] Integration test in `runner/tests/integration/test_checkpoint_fingerprint_recheck.py`: pass a gate, mutate the working tree before `checkpoint`, assert `checkpoint` fails with **exactly** `CHECKPOINT BLOCKED — WORKING TREE CHANGED AFTER GATE` and leaves the index untouched; revert the mutation, re-gate, and confirm `checkpoint` then succeeds (depends on: T040)
- [ ] T044 [P] [US2] Integration test in `runner/tests/integration/test_candidate_tree_invariant_chain.py` asserting each link of `Candidate Tree X → Fingerprint X → staged tree X → checkpoint commit tree X` independently, plus that untracked-non-ignored files and deletions are captured while `.ai-runs/` content is not (depends on: T041)
- [ ] T045 [P] [US2] Integration test in `runner/tests/integration/test_staging_precondition.py`: a stray manual `git add` before `run-codex-gate`/`checkpoint` is refused (exit `2`), names the unexpectedly staged path, and `git diff --cached --name-only` still shows it afterward — the runner never ran `git reset` or any other index-mutating recovery on its own (depends on: T039, T040)
- [ ] T046 [US2] Implement `solari-workflow resume`: refuse unless block-state is `BLOCKED_MANUAL` or `ARCHITECTURAL_DECISION_REQUIRED`; revalidate the recorded branch/HEAD (and, if `last_safe_stage = GATE_PASSED`, the recorded Fingerprint X against a freshly rebuilt Candidate Tree exactly as `checkpoint` would); on success set `READY_TO_RESUME` and continue at the first incomplete stage after `last_safe_stage` without re-running an already-complete stage; on revalidation failure, stop with an actionable explanation rather than restarting from zero (depends on: T031, T037, T038, T039)
- [ ] T047 [P] [US2] Integration tests in `runner/tests/integration/test_retry_and_resume.py`: a simulated operational failure at each `last_safe_stage` value triggers exactly one automatic retry and then `BLOCKED_MANUAL` if it recurs; `resume` then revalidates and continues from the correct stage **without** re-invoking the fake Claude stub when implementation was already `IMPLEMENTATION_COMPLETE` or later (research.md §18, quickstart Scenario 7) (depends on: T046)
- [ ] T048 [US2] Wire the Claude/Codex Git-prohibition contract into `run-claude`/`run-codex-gate`'s own invocation setup: apply the tightest tool-permission profile the installed `claude`/`codex` CLI exposes where supported (verified against the real installed CLI's capability, not assumed), and document in code comments that the fingerprint recheck (T040/T043) and staging precondition (T045) are the technical detection backstop when no such profile is available (depends on: T033, T035)
- [ ] T049 [US2] Implement and test the manual-only push policy as an explicit absence: confirm (via `runner/tests/unit/test_no_push_capability.py`) that `cli.py` exposes no `push` subcommand, and that no module under `git/` ever constructs a `git push` argv — a static-scan assertion test guarding against a future accidental addition (depends on: T020, T040)
- [ ] T050 [US2] Implement `solari-workflow status`: read-only report of current lock ownership (if any), the full contents of `.ai-runs/.block-state.json` (if a block is active), and whether a checkpoint would currently be blocked — never mutates anything (depends on: T015, T031)

**Checkpoint**: One block can be fully implemented, reviewed, and
checkpointed end-to-end with every required invariant technically enforced
and tested — including remediation, resume, and the manual-push boundary.

---

## Phase 5: User Story 3 - Prevent concurrent or unauthorized execution (Priority: P1)

**Goal**: Every subcommand acquires the run lock for its own duration; a
second execution against the same working tree is refused immediately with
actionable ownership detail — never queued, never silently retried.

**Independent Test**: Start one execution, then attempt a second against the
same project, and observe the second is refused rather than allowed to run
concurrently.

- [ ] T051 [US3] Wire `lock/run_lock.py` acquire/release into every CLI subcommand's dispatch in `cli.py` (`start-block`, `run-claude`, `run-codex-gate`, `checkpoint`, `resume`, `retention`), recording tool/model/effort/session_mode/scope/purpose on acquisition and releasing in a `finally` regardless of outcome (depends on: T015, T020, T037, T038, T039, T040, T046)
- [ ] T052 [P] [US3] Integration test in `runner/tests/integration/test_lock_contention.py`: spawn a real second `python -m solari_workflow` process holding the lock while the main test process asserts a concurrent second acquisition attempt is refused immediately, naming the first invocation's tool/pid/hostname/purpose (research.md §7, quickstart Scenario 5) (depends on: T051)
- [ ] T053 [P] [US3] Unit tests for stale-lock liveness annotation in `runner/tests/unit/test_lock_liveness.py`: process-alive / process-not-alive / liveness-undeterminable message variants, and confirmation the runner never auto-removes another process's lock file (depends on: T015)

**Checkpoint**: Concurrent or unauthorized execution against the same
working tree is technically refused, not merely documented.

---

## Phase 6: User Story 4 - Validate project readiness before a block starts (Priority: P2)

**Goal**: The Readiness Gate becomes fully conditional on the project's
actual stack — Python/Node/Go/Rust checks plus conditional Docker and
local-model sections run only when applicable, and a genuinely missing
condition is reported specifically rather than the gate failing (or passing)
silently.

**Independent Test**: Deliberately omit one readiness condition (e.g. a
required lockfile present on disk but untracked by Git) and confirm the gate
reports that specific, actionable failure and blocks progression.

- [ ] T054 [P] [US4] Minimal fixture project trees under `runner/tests/fixtures/projects/{python,node,go,rust}/` (manifest present/absent, lockfile present/absent/untracked variants) per research.md §18
- [ ] T055 [US4] Implement the stack registry dispatch in `readiness/engine.py`: `{"python": python_stack.CHECKS, "node": node_stack.CHECKS, "go": go_stack.CHECKS, "rust": rust_stack.CHECKS}` selected by `[environment].stack`; `"other"` runs common checks only (depends on: T018)
- [ ] T056 [P] [US4] Implement `readiness/checks/python_stack.py` (manifest/lockfile presence-and-tracked, runtime-version declared) (depends on: T055)
- [ ] T057 [P] [US4] Implement `readiness/checks/node_stack.py` (same shape as T056, Node-specific manifest/lockfile) (depends on: T055)
- [ ] T058 [P] [US4] Implement `readiness/checks/go_stack.py` (same shape as T056, Go-specific manifest/lockfile) (depends on: T055)
- [ ] T059 [P] [US4] Implement `readiness/checks/rust_stack.py` (same shape as T056, Rust-specific manifest/lockfile) (depends on: T055)
- [ ] T060 [US4] Implement `readiness/checks/docker.py`: conditional on `[environment.docker]` presence — fails only on a non-pinned/`latest` image tag, reports `SKIPPED` (never `FAIL`) when the table is absent (depends on: T055)
- [ ] T061 [US4] Implement `readiness/checks/local_model.py`: conditional on `[environment.local_model]` presence — validates `runtime`/`runtime_version`/`model_id` (and `digest` if present), reports `SKIPPED` when the table is absent (depends on: T055)
- [ ] T062 [US4] Implement `solari-workflow readiness-check` (read-only, prints the full `CheckResult` list including `SKIPPED` entries) and integrate the now-complete stack registry into `start-block`'s existing readiness call (depends on: T037, T055)
- [ ] T063 [P] [US4] Integration tests in `runner/tests/integration/test_readiness_stack_matrix.py`: each stack's fixture combinations produce the correct `PASS`/`FAIL`/`SKIPPED`; a project with no Docker and no local model produces zero failures attributable to those sections (spec.md SC-006); an untracked-but-present lockfile produces the specific documented failure and blocks `start-block` (User Story 4 Acceptance Scenario 2) (depends on: T054, T056, T057, T058, T059, T060, T061, T062)

**Checkpoint**: The Readiness Gate is fully stack-conditional — no project
fails on a genuinely inapplicable section, and a real missing condition is
reported specifically.

---

## Phase 7: User Story 5 - Retain an auditable, bounded history of branches and checkpoints (Priority: P3)

**Goal**: After several blocks are checkpointed, retention keeps the two
most recently merged development branches plus the active branch, never
touches checkpoint tags or the active branch, and only ever safe-deletes.

**Independent Test**: Checkpoint three or more blocks in sequence and
confirm only the two most recent merged branches plus the active branch
remain, while every checkpoint tag from all blocks still exists.

- [ ] T064 [US5] Implement `runner/src/solari_workflow/git/retention.py`: enumerate merged dev branches matching `T\d+-[A-Za-z0-9]+` (`git branch --merged main` + a `merge-base --is-ancestor` double-check per candidate), unconditionally exclude the active branch, sort remaining candidates by tip-commit committer date descending, keep the top 2, safe-delete (`git branch -d`, never `-D`) the rest, never touch tags or `main`, and recompute statelessly every run (no persisted retention ledger) (depends on: T010, T011)
- [ ] T065 [US5] Implement `solari-workflow retention [--dry-run]` and wire retention as `checkpoint`'s final step (the call site already stubbed in T040) (depends on: T064, T040)
- [ ] T066 [P] [US5] Integration test in `runner/tests/integration/test_branch_retention.py`: checkpoint 3+ blocks in sequence, confirm exactly the 2 most recently merged branches plus the active branch remain, all N checkpoint tags are still present, the active branch is never a removal candidate even if it is the oldest, and deletion never escalates to force (spec.md SC-005, quickstart Scenario 6) (depends on: T065)

**Checkpoint**: All five user stories are independently functional — the
full v1 block lifecycle, its safety invariants, and cross-project
portability are complete.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Consistency, portability confidence, and documentation across
everything built above.

- [ ] T067 [P] Deterministic error/reporting contract pass: audit every subcommand's exit codes (`0`/`1`/`2`) and stderr message shapes against `contracts/cli-interface.md` for consistency across `runner/src/solari_workflow/cli.py` and its subcommand modules; fill any gap found
- [ ] T068 [P] Cross-platform CI matrix (`ubuntu-latest` / `windows-latest` / `macos-latest`) running the full `uv run pytest` suite on all three, per research.md §18's SHOULD
- [ ] T069 [P] Documentation pass: finalize the runner's own `runner/README.md` (install, `init`, full block-lifecycle walkthrough) and cross-link `docs/architecture/` references, `contracts/config-schema.md`, and `contracts/cli-interface.md` where they've drifted from the implemented behavior
- [ ] T070 Execute quickstart.md Scenarios 2–8 end-to-end against the completed runner and fake stubs; record results and fix any discrepancy found against the documented expectation
- [ ] T071 [P] Final full validation: run the complete `uv run pytest` suite (unit + integration) to green on the primary development platform and record the total test count

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — starts immediately.
- **Foundational (Phase 2)**: Depends on Setup. **Blocks every user story.**
- **User Story 1 (Phase 3)**: Depends only on Foundational (config loader,
  CLI skeleton). Fully independent of Phases 4–7.
- **User Story 2 (Phase 4)**: Depends on Foundational, including the
  common-checks-only readiness engine (T018) — does **not** require Phase 6
  (US4)'s stack-specific registry to be independently testable, since its own
  test uses a stack-agnostic fixture.
- **User Story 3 (Phase 5)**: Depends on Foundational's lock module (T015)
  and, for its integration test, on at least one Phase 4 subcommand existing
  to wrap (T051 wires into `start-block`/`run-claude`/`run-codex-gate`/
  `checkpoint`/`resume`/`retention`). Practically sequenced after Phase 4 and
  before/alongside Phase 7 since `retention` is one of the commands it wraps.
- **User Story 4 (Phase 6)**: Depends on Foundational's readiness engine
  (T018) and, for its `start-block` integration point, on T037 (Phase 4).
  Its own stack/registry/check work is otherwise independent of Phases 4/5/7.
- **User Story 5 (Phase 7)**: Depends on Foundational's Git ops/branch
  naming (T010/T011) and on Phase 4's `checkpoint` (T040), since retention is
  wired as checkpoint's final step.
- **Polish (Phase 8)**: Depends on all preceding phases being complete.

**Net effect**: Phase 2 is the only hard gate for everything. Phase 3 (US1)
can proceed fully in parallel with Phases 4–7 once Phase 2 is done. Phases 4,
6, and 7 have a real, spec-driven sequencing constraint (`start-block` calls
readiness; `checkpoint` calls retention) that keeps them from being fully
parallel despite being separate user stories — this is called out explicitly
rather than glossed over, per the instruction not to claim independence the
domain doesn't actually have. Phase 5 (US3) is a thin, cross-cutting wrapper
best done once the commands it wraps (Phase 4, and ideally Phase 7) exist.

### Recommended Execution Order

1. Phase 1 → Phase 2 (strict)
2. Phase 3 (US1) — can run fully in parallel with step 3 by a second
   implementer once Phase 2 is done
3. Phase 4 (US2) → Phase 6 (US4, registry work can start in parallel with
   Phase 4 but its `start-block` integration task T062 waits on T037) →
   Phase 7 (US5, waits on T040) → Phase 5 (US3, wraps the commands from
   Phases 4/6/7)
4. Phase 8 (Polish) — last

### Within Each User Story

- Fixtures/fixtures-adjacent tests before or alongside the implementation
  they exercise.
- Lower-level modules (Candidate Tree, Fingerprint, block state, actors)
  before the CLI subcommands that compose them.
- Integration tests for a subcommand after that subcommand and its
  collaborators exist.

### Parallel Opportunities

- All `[P]`-marked Setup tasks (T003) and Foundational tasks (T005, T006,
  T007, T009, T011, T012, T014, T017, T019, T021) can run in parallel with
  each other once their own listed dependencies are met.
- Within US2, the three independent primitive tracks — Candidate Tree/
  Fingerprint (T027–T030), block state (T031–T032), and the two actor
  modules (T033–T036) — can be built in parallel by different implementers;
  the CLI subcommands (T037–T040) fan in from all three.
- Within US4, the four stack-check modules (T056–T059) are fully parallel
  once the registry dispatch (T055) exists.
- Across stories: US1 (Phase 3) is parallel with everything else once
  Foundational is done; US3's wiring task (T051) and US5's retention module
  (T064) are parallel with each other, both gated on Phase 4.

---

## Parallel Example: Phase 2 Foundational

```bash
# Once T005 exists, these can run together:
Task: "Unit tests for platform/proc.py in runner/tests/unit/test_proc.py"
Task: "Implement config dataclasses in runner/src/solari_workflow/config/schema.py"
Task: "Implement git/branch.py PascalCase derivation and name parsing"
```

## Parallel Example: User Story 2 primitives

```bash
# All three tracks below are independent of each other and can proceed
# in parallel once Phase 2 is complete:
Task: "Implement git/candidate_tree.py isolated temporary-index construction"
Task: "Implement state/block_state.py .ai-runs/.block-state.json schema"
Task: "Implement actors/claude.py STATUS: contract parsing"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (critical — blocks every story)
3. Complete Phase 3: User Story 1 (bootstrap)
4. **STOP and VALIDATE**: run quickstart.md Scenario 1 against a real
   throwaway project of a different stack
5. Report to Orchestrator before proceeding to Phase 4

### Incremental Delivery

1. Setup + Foundational → foundation ready
2. User Story 1 → validate independently (bootstrap works generically)
3. User Story 2 → validate independently (one block, full lifecycle,
   against the stack-agnostic fixture) — this is the core value proposition
4. User Story 3 → validate independently (concurrency refusal)
5. User Story 4 → validate independently (stack-conditional readiness) and
   confirm it does not regress User Story 2's own test
6. User Story 5 → validate independently (branch retention) and confirm it
   does not regress User Story 2's checkpoint test
7. Polish → cross-platform CI, docs, full-suite green run

Each user story is a genuine increment of value; Phases 4/6/7's noted
sequencing constraint (not full parallel independence) is a property of the
domain (the spec's own required call sequence: readiness → block → gate →
checkpoint → retention), not a modeling shortcut.
