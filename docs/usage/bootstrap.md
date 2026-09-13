# Bootstrap a Project onto the Workflow

This walks through adopting the Solari AI Spec Kit Workflow on a project
that doesn't have it yet — from nothing installed on the machine to a
scaffolded `.ai-workflow.toml`. It implements User Story 1 (spec.md) and
research.md §17's installation/bootstrap decision.

Every step here is something a human (or an Orchestrator acting on a
human's behalf) runs directly in a terminal — none of it is performed by
Claude Code or Codex, per the canonical responsibility model
(research.md §0.0).

## 1. Prerequisite: `uv`

[`uv`](https://docs.astral.sh/uv/) is the one thing that must already be
on the machine. It provisions Python itself — there is no separate "install
Python 3.12" step, and no assumption that the target OS already ships a
suitable Python (research.md §17 point 2, FR-038).

Install it via the official Astral installer for your OS (see
[the `uv` installation docs](https://docs.astral.sh/uv/getting-started/installation/)
for the exact, current command — it differs by platform and changes
faster than this document should try to track). Verify it landed on
`PATH`:

```bash
uv --version
```

## 2. Prerequisite: GitHub authentication (private repository)

This workflow's own repository is private, so installing it requires
GitHub authentication configured for `git`/`uv` to use when cloning over
`https://github.com/...` (an SSH remote works too, if that's already your
default). The exact mechanism (a personal access token via `gh auth
login`, an SSH key, or a credential helper) is up to you — `uv tool
install --from git+https://...` shells out to `git` under the hood, so
whatever already lets you `git clone` this repository also lets `uv`
install it.

## 3. Install the runner

**This repository's installable Python project lives under `runner/`, not
the repository root** (there is no `pyproject.toml` at the top level —
see `docs/architecture/repository-structure.md`). `uv` installs directly
from a subdirectory of a Git checkout via the same `#subdirectory=<path>`
fragment `pip` recognizes; this exact form was verified locally against a
disposable repository mirroring this layout (a local, non-published `git
+file://` remote) before being documented here — see "Verified outcome"
below.

Recommended: an isolated tool install, fully separate from whatever
environment your own project's stack uses (Go, Node, Rust, Python, or
otherwise):

```bash
uv tool install --from "git+https://github.com/fabiofilz/solari-ai-speckit-workflow@<tag>#subdirectory=runner" solari-workflow
```

Replace `<tag>` with the specific released version you intend to adopt —
never an unpinned branch reference, so a bootstrap is always reproducible
(research.md §17 point 3). Quote the whole `--from` value, since the URL
contains a `#` the shell would otherwise treat as a comment. This places a
`solari-workflow` executable on `PATH`.

**Contributor/local-clone alternative** — for working on the runner
itself, or trying an unreleased change (note the trailing `/runner` —
`--project` must point at the directory that actually contains
`pyproject.toml`):

```bash
git clone https://github.com/fabiofilz/solari-ai-speckit-workflow.git
uv run --project solari-ai-speckit-workflow/runner solari-workflow ...
```

**Upgrading** later is always explicit, never silent (research.md §17
point 6): re-run the same `uv tool install ... --force` against a new
`<tag>`, then deliberately bump `[workflow] version` in your project's own
`.ai-workflow.toml`. A runner/config version mismatch is only ever an
informational warning at readiness-gate time — never auto-corrected.

## 4. Scaffold `.ai-workflow.toml`

From your project's root:

```bash
solari-workflow init --project-name <your-project-name> --stack python|node|go|rust|other
```

This writes a schema-valid `.ai-workflow.toml` using generic,
stack-conventional defaults (e.g. Go gets `go.mod`/`go.sum`, Node gets
`package.json`/`package-lock.json`) — never a value guessed from, or
copied out of, this workflow's own reference project
(`runner/src/solari_workflow/config/init.py`, spec.md SC-001). It also
ensures `.ai-runs/` is present in `.gitignore` (research.md §8's audit
trail directory must never be tracked by Git).

Useful flags:

- `--project-root <path>` — target a directory other than the current one
  (mainly for scripted/test bootstraps).
- `--project-type`, `--main-branch`, `--spec-dir` — override the generic
  defaults (`"service"`, `"main"`, `"specs"`).
- `--runtime-version`, `--dependency-manager`, `--manifest`, `--lockfile`
  — override the generic per-stack defaults with your project's actual
  values, when you have them; left unset, `init` fills in the stack's
  conventional default rather than guessing something project-specific.
- `--force` — required to overwrite an existing `.ai-workflow.toml`; a
  unified diff is shown before anything is written.

Exit codes: `0` success; `1` a config already exists and `--force` was not
given (`contracts/cli-interface.md`).

Review the generated file afterward and adjust anything the generic
defaults got wrong for your project — `init` produces a valid starting
point, not a final answer.

## 5. Spec Kit's own initialization — a separate step

This workflow **reuses** Spec Kit's own lifecycle rather than
reimplementing it (FR-013). If your project hasn't already run Spec Kit's
own bootstrap, do that separately:

```bash
specify init
```

...followed by whichever `/speckit.*` commands (`/speckit.constitution`,
`/speckit.specify`, `/speckit.plan`, `/speckit.tasks`, ...) your workflow
normally uses to produce `spec.md`/`plan.md`/`tasks.md` for a feature.
`solari-workflow`'s own commands (`start-block`, `run-claude`,
`run-codex-gate`, `checkpoint`, ...) consume the task list Spec Kit
produces — they never generate or edit it themselves
(`speckit/integration.py`).

## Verified outcome (quickstart.md Scenario 1)

**Corrected (T022-T032 re-gate, Finding 3 — MAJOR)**: an earlier version
of this document showed installation commands targeting the repository
root, which has no `pyproject.toml` — and T025's original disposable test
used `uv run --project .../runner` directly, which never actually
exercised the documented top-level path at all. Both the documented
install command (Section 3's `#subdirectory=runner` form) and the
local-clone alternative are now verified against the same disposable
setup below, so documentation and tested path are identical.

Because the real GitHub remote is private and not reachable from this
verification environment, the `#subdirectory=runner` install path was
validated against a **local, disposable Git repository that faithfully
mirrors this repository's actual layout** (a `git+file://` remote in
place of `git+https://github.com/...` — `uv`/`git` treat both transports
identically once cloned; only the URL scheme differs):

```bash
# Disposable stand-in for this repository, tagged like a real release —
# NOT the real project repository (never used here):
cp -R <checkout-of-this-repo>/runner disposable-repo/runner
cd disposable-repo && git init -q -b main && git add -A && git commit -q -m snapshot
git tag v0.0.0-test

# The EXACT form documented in Section 3, with the URL scheme swapped:
uv tool install --from "git+file://$(pwd)@v0.0.0-test#subdirectory=runner" solari-workflow

# Then, against a separate disposable throwaway project:
cd /tmp/throwaway-go-project && git init -q -b main
solari-workflow init --project-name throwaway-go-project --stack go
```

**Result**: `uv tool install` with the `#subdirectory=runner` fragment
resolved, built, and installed `solari-workflow` successfully from the
disposable repo's `runner/` subdirectory — confirming the syntax
documented in Section 3 is real and correct, not merely plausible.
`solari-workflow init --project-name throwaway-go-project --stack go`
then produced `.ai-workflow.toml` with `environment.stack = "go"` and
`[git.push] mode = "manual"`, using the generic Go defaults
(`go.mod`/`go.sum`/Go modules) — no PDF-Converter-specific value anywhere
in the file (verified by grepping the output for reference-project
markers). `.ai-runs/` was added to `.gitignore`. A second `init` without
`--force` was refused (exit `1`) without touching the existing file; the
same command with `--force` printed a unified diff before overwriting.
Matches spec.md SC-001 and quickstart.md Scenario 1's documented
expectation exactly. The tool was uninstalled and the disposable repo
removed immediately afterward.

The local-clone alternative (`uv run --project
<checkout>/runner solari-workflow ...`) was separately verified the same
way, against a real checkout of this repository's `runner/` directory —
this is the form used by `runner/tests/integration/test_init_command.py`
and by this project's own development workflow.

No Git command was run against this repository's own working tree during
either verification — only against the disposable stand-ins above and
throwaway project directories.
