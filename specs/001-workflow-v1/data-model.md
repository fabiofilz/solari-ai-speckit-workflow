# Phase 1 Data Model: AI Spec Kit Workflow v1 Runner

This is a **configuration and audit-record** data model, not an
application database — every entity below is either a section of the
`.ai-workflow.toml` project config, a Git object/ref/tag, or a flat file
under `.ai-runs/`. See `research.md` for the mechanisms and
`contracts/config-schema.md` for the field-by-field TOML reference.

**User vs. Orchestrator**: the User (a human) and ChatGPT/the Orchestrator
are distinct actors (research.md §0.0) but neither is modeled as a data
entity here — the User's request ("implement the next block") and the
Orchestrator's resulting decisions (task range, block name, model, effort,
session mode) reach the Runner only as CLI arguments to the commands
below; nothing about the User is persisted. Every field below marked
"Orchestrator-supplied" or "Orchestrator-selected" is a decision the
Orchestrator made in response to a User request, never one the Runner
made or the User specified directly to the Runner.

---

## Project Workflow Configuration

One `.ai-workflow.toml` per consuming project, at the project root.
**Owned by ChatGPT/the orchestrator** (FR-015); Claude Code and Codex read
it and MUST NOT modify it during ordinary implementation/review runs
(constitution Governance). The runner only reads it (research.md §3/§4).

| Field (TOML path) | Type | Required? | Notes |
|---|---|---|---|
| `workflow.schema_version` | string | **Required** | Only `"1"` recognized in v1; fail-fast on anything else (research.md §5). |
| `workflow.source` | string (URL) | **Required** | Canonical repo URL this project was bootstrapped against. |
| `workflow.version` | string (semver) | **Required** | Tagged release last bootstrapped/upgraded to. |
| `project.name` | string | **Required** | Generic — no hard-coded reference-project value ever ships in this repo's own templates (FR-016). |
| `project.type` | string | **Required** | Free-text artifact/project type (e.g. `"service"`, `"library"`, `"cli"`); informs nothing structurally in v1 beyond documentation. |
| `project.main_branch` | string | **Required** | Target of checkpoint merges. |
| `speckit.spec_dir` | string (path) | **Required** | Root of Spec Kit artifacts (default `"specs"`). |
| `speckit.status` | string | Optional | Free-text lifecycle marker for human/orchestrator reference. |
| `environment.stack` | enum: `python\|node\|go\|rust\|other` | **Required** | Selects the readiness-check registry entry (research.md §11). `"other"` runs only common checks. |
| `environment.runtime_version` | string | **Required** | e.g. `"3.12"`, `"1.22"`, `"20"`, `"1.79"` — exact meaning is stack-dependent. |
| `environment.dependency_manager` | string | **Required** | e.g. `"uv"`, `"npm"`, `"go modules"`, `"cargo"`. |
| `environment.manifest` | string (path) | **Required** | e.g. `"pyproject.toml"`, `"package.json"`, `"go.mod"`, `"Cargo.toml"`. |
| `environment.lockfile` | string (path) | Stack-conditional | Required when the stack has a lockfile concept; readiness fails if missing/untracked (FR-020). |
| `environment.lockfile_must_be_committed` | bool | **Required** | Must be `true` for v1 (Reproducible Environments); present explicitly rather than assumed. |
| `environment.docker` | table | Optional | Present only if the project uses Docker/Compose. |
| `environment.docker.images` | array of strings | Required if table present | Each entry MUST be a pinned, non-`latest` tag (readiness check, FR-018). |
| `environment.docker.compose_file` | string (path) | Optional | Authoritative source for image tags — README/docs reference this instead of restating versions (FR-019). |
| `environment.local_model` | table | Optional | Present only if the project depends on a local model (e.g. Ollama). |
| `environment.local_model.runtime` | string | Required if table present | e.g. `"ollama"`. |
| `environment.local_model.runtime_version` | string | Required if table present | Pinned version. |
| `environment.local_model.model_id` | string | Required if table present | Model name/tag. |
| `environment.local_model.digest` | string | Optional | Only when the platform exposes a digest/hash. |
| `environment.os_dependencies` | array of tables `{name, version}` | Optional | Direct OS-installed prerequisites documented with exact/supported versions. |
| `environment.env_vars` | array of tables `{name, example}` | Optional | Safe example values only — the runner treats any value here as documentation, never a secret; a heuristic secret-scan (research.md §11) still flags suspicious values. |
| `build.test_command` | array of strings (argv) or string | Optional | Tokenized/executed per research.md §6; absent → readiness `SKIPPED`, not `FAIL`, if genuinely inapplicable. |
| `build.build_command` | array of strings or string | Optional | Same handling as `test_command`. |
| `build.lint_command` | array of strings or string | Optional | Same handling as `test_command`. |
| `git.checkpoint_merge_strategy` | enum: `"no-ff"` | **Required** | Only `"no-ff"` supported in v1 — present explicitly so a future value change is visible, not silently assumed. |
| `git.push.mode` | enum: `"manual"` | **Required** | v1 recognizes only `"manual"` — no `solari-workflow` subcommand ever pushes (FR-031, research.md §0.2). An invalid value for this known field is a hard validation failure (not covered by the unknown-*key* leniency rule in `contracts/config-schema.md`). Future workflow versions may add modes without reshaping this table. |
| `checkpoint.tag_prefix` | string | **Required** | Must be `"checkpoint"` in v1 (fixed by spec/constitution naming convention). |
| `checkpoint.blocking_severities` | array of strings | **Required** | Default `["blocker", "major"]`; decides which Codex finding severities block checkpoint eligibility (research.md §10). |
| `branch_retention.keep_recent_merged` | integer | **Required** | Default `2` (FR-034). |
| `branch_retention.keep_active` | bool | **Required** | Must be `true` (FR-034/FR-035 — the active branch is never a retention candidate). |
| `ai_runs.directory` | string (path) | **Required** | Default `".ai-runs"`. |
| `ai_runs.git_ignored` | bool | **Required** | Must be `true`; readiness fails if the directory is tracked by Git. |
| `known_deferred` | array of tables `{id, reason, owner}` | Optional | Project-specific known expected failures/deferred owners (FR-017), explicitly configured — never inferred. |

**Validation summary** (full rules in research.md §5 and
`contracts/config-schema.md`): missing/malformed required fields and an
unrecognized `schema_version` are hard failures, reported in aggregate;
unrecognized keys/tables anywhere are warnings, not failures; stack-
conditional and optional tables are validated only when present.

---

## Environment Contract

Not a separate file — it is the `[environment]` table (and its optional
sub-tables) of the Project Workflow Configuration above. Modeled as a
sub-entity here only because the spec calls it out as a distinct Key
Entity; there is no second schema to maintain, which itself satisfies
FR-019 ("avoid duplicating an authoritative source of truth").

| Concept (spec Key Entity) | Config location | Applicability |
|---|---|---|
| Language/runtime + version | `environment.runtime_version` | Always required. |
| Dependency manager + manifest | `environment.dependency_manager`, `environment.manifest` | Always required. |
| Dependency lockfile (committed) | `environment.lockfile`, `environment.lockfile_must_be_committed` | Required when the stack has a lockfile concept. |
| Docker/Compose, pinned tags | `environment.docker.*` | Conditional — present only if used. |
| OS-installed dependencies | `environment.os_dependencies` | Conditional. |
| Local services / local model | `environment.local_model.*` | Conditional. |
| Architecture/OS assumptions | `environment.os_dependencies` entries or `project.type` free text | Conditional, documented inline. |
| Env vars (safe examples only) | `environment.env_vars` | Conditional; never a secret value. |

---

## Readiness Gate Result

Produced fresh by every `readiness-check` invocation; not persisted as its
own file format beyond being echoed into the next `.ai-runs/*-result.md`
that consumes it (e.g. a subsequent implementation run's prompt file notes
the readiness result that authorized it).

| Field | Type | Notes |
|---|---|---|
| `overall_status` | enum: `FAIL\|INCOMPLETE\|PASS` | Precedence `FAIL > INCOMPLETE > PASS`: `FAIL` if any executed check is `FAIL`; else `INCOMPLETE` if a required stack-specific readiness implementation is missing or empty; else `PASS` (all required common and registered stack-specific checks exist and pass — `[environment].stack = "other"` may use common checks alone and therefore produces normal `PASS`/`FAIL`). |
| `checks` | list of `CheckResult` | Always includes `SKIPPED` entries for transparency. |
| `checked_at` | datetime (UTC) | |
| `config_version` | string | The `workflow.version` the config declared, for cross-reference. |

`CheckResult`: `{name: str, status: PASS|FAIL|SKIPPED, message: str,
severity: info|warning|blocking}` (research.md §11).

---

## Development Block

A logical grouping, not a stored object of its own — identified entirely
by its Git branch name and the Spec Kit task-ID range it covers. Created
**only** by the runner's `start-block` command from an orchestrator-
supplied definition (research.md §0.1/§0.10) — never inferred or invented
by the runner itself.

| Field | Derived from |
|---|---|
| `first_task_id`, `last_task_id` | Orchestrator-selected task range (from `tasks.md`). |
| `block_name` | Orchestrator-supplied, validated at `start-block`: 1-80 ASCII letters/digits separated by single spaces, `-`, `_` or `.`, starting and ending with a letter or digit and containing a letter (no line breaks, control characters or `:` — it is rendered into line-oriented Git messages). |
| `branch_name` | `T{first_task_id}-{PascalCase(block_name)}` (research.md §16). |
| `task_ids` | Full list of task IDs in the range, for tag metadata (research.md §14). |

---

## Execution Record

One per `claude`/`codex` invocation, materialized as an `.ai-runs/`
prompt/result file pair (research.md §8) plus the transient run-lock
payload (research.md §7) held while it executes.

| Field | Source |
|---|---|
| `tool` | `"claude"` \| `"codex"` |
| `model` | Orchestrator-supplied. |
| `effort` | Orchestrator-supplied. |
| `session_mode` | `"NEW"` \| `"CONTINUE"` (`"CONTINUE"` valid for `claude` only — never for `codex`, research.md §10). |
| `prior_session_id` | Present only when `session_mode = "CONTINUE"`. |
| `speckit_involved` | bool. |
| `scope` | Task IDs / block name. |
| `purpose` | Free text. |
| `started_at` / `ended_at` | UTC timestamps. |
| `status` | For `claude`: `COMPLETE \| ARCHITECTURAL_DECISION_REQUIRED \| BLOCKED` (research.md §9), fail-closed to `BLOCKED` if unparseable. For `codex`: not applicable — see `result_summary`. |
| `result_summary` | For a gate: `PASS`/`FAIL` + findings; for implementation: free-text self-validation summary. |
| `fingerprint` | Present only on a Codex gate result — a **Fingerprint** record (below), not a bare hash (research.md §12). |

---

## Fingerprint (Candidate Tree binding)

**Corrected from the original draft**, which assumed a single SHA-256
digest over an already-committed tree. See research.md §12 for the full
construction (an isolated temporary Git index, never the real one).
Structurally a small record, not one opaque hash — its component OIDs are
themselves the authoritative values compared for equality:

| Field | Type | Notes |
|---|---|---|
| `head_oid` | string (Git OID) | `git rev-parse HEAD` at compute time. |
| `candidate_tree_oid` | string (Git OID) | Result of `git write-tree` against the temporary index seeded from `HEAD` and `git add -A`'d from the working directory (research.md §12) — this **is** the Candidate Tree Codex reviewed. |
| `branch_name` | string | Branch the Candidate Tree was built on. |
| `first_task_id`, `last_task_id` | string | Block/task identity, for audit binding (research.md §12). |
| `computed_at` | datetime (UTC) | |

Equality for both the before/after-Codex check (research.md §10) and the
pre-checkpoint recheck (research.md §13) means `head_oid` **and**
`candidate_tree_oid` both match — never a derived composite hash alone.

Stored in the gate's immutable `.ai-runs/*-result.md` (authoritative) and
mirrored into `.ai-runs/.block-state.json`'s `last_gate` field (disposable,
regenerable — research.md §0.7).

---

## Block State

**New entity** (research.md §0.7) — the small persistent record that lets
an interrupted block resume safely instead of restarting blindly. Lives at
`.ai-runs/.block-state.json`; a fast-path cache, not the sole source of
truth (regenerable from `.ai-runs/*-result.md` history and current Git
state if lost).

| Field | Type | Notes |
|---|---|---|
| `branch_name`, `first_task`, `last_task`, `block_name` | string | The active Development Block's identity. |
| `state` | enum | `RUNNING \| QA_REMEDIATION_REQUIRED \| RETRYABLE_ERROR \| BLOCKED_MANUAL \| ARCHITECTURAL_DECISION_REQUIRED \| READY_TO_RESUME \| COMPLETED` (research.md §0.7) — the entire state vocabulary; no other values exist. |
| `last_safe_stage` | enum | `BRANCH_CREATED \| IMPLEMENTATION_COMPLETE \| CANDIDATE_TREE_BUILT \| GATE_PASSED \| CHECKPOINT_COMPLETE` — the last stage proven to have completed correctly; `resume` continues immediately after this stage, never before it. |
| `retry_count_current_stage` | integer (`0` or `1`) | Fixed ceiling of `1` (research.md §0.6) — not configurable. |
| `last_gate` | Fingerprint record or `null` | Populated once a `PASS` gate exists for the current branch. |
| `updated_at` | datetime (UTC) | |

---

## Checkpoint

Not a file — a Git state: an annotated tag plus the merge commit it points
at (via `main`), whose tree is proven equal to the Candidate Tree the gate
reviewed (research.md §13's invariant chain).

| Field | Source |
|---|---|
| `tag_name` | `checkpoint-T{first}-T{last}`. |
| `checkpoint_commit_oid` | Created via `git commit-tree <candidate_tree_oid> -p <head_oid>` (research.md §13) — **not** `git commit` — so its tree equals `candidate_tree_oid` by construction, verified via `git rev-parse --verify <oid>^{tree}`. |
| `merge_commit_sha` | Result of `git merge --no-ff` of the block branch (now including the checkpoint commit) into `main`. |
| `block_name`, `branch_name`, `task_range`, `task_titles` | From the Development Block + `tasks.md` lookup (research.md §14). |
| `gate_result` | Always `PASS` (a checkpoint cannot exist otherwise). |
| `checkpoint_status` | Always `VERIFIED`. |
| `optional_metadata` | Test counts / known deferred failures / lint / determinism — included only if already available (research.md §14). |

---

## Run Lock

Ephemeral, not historical — deleted on release. See research.md §7 for the
full field list, lifecycle, and its distinction from Block State (a run
lock covers one process's duration; Block State persists across the
several invocations that make up one block). Included here for
completeness of the data model:

`{pid, hostname, tool, model, effort, session_mode, scope, purpose,
started_at, lock_schema_version}`, stored at `.ai-runs/.lock`.

---

## Entity Relationships

```
Project Workflow Configuration ──contains──> Environment Contract
Project Workflow Configuration ──authorizes──> Readiness Gate Result
Development Block ──tracked by──> Block State
Development Block ──implemented via──> Execution Record (tool=claude, status=COMPLETE)
Development Block ──reviewed via──> Execution Record (tool=codex) ──produces──> Fingerprint (Candidate Tree binding)
Fingerprint ──re-verified at──> Checkpoint (invariant chain: Candidate Tree → Fingerprint → staged tree → commit tree)
Development Block ──becomes──> Checkpoint (branch + tag)
Run Lock ──owns──> (at most one) Execution Record, for its duration
Block State ──enables──> resume (continues after last_safe_stage, never re-running completed work)
```
