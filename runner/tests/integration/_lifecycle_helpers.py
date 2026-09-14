"""Shared setup helpers for the block-lifecycle integration tests
(T041-T045, T047) - not itself a test module (no `test_` prefix, so
pytest's own discovery per `pyproject.toml`'s `python_files` pattern
never collects it).

Every helper here operates against a real, disposable temporary Git
repository - never the real project repository - matching research.md
§18's "run against real `git` CLI in temporary repositories" strategy,
and drives the real CLI entry point (`solari_workflow.cli.main`) rather
than calling internal functions directly, so these tests exercise the
same code path an Orchestrator's own subprocess invocation would.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from solari_workflow.cli import main

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FAKE_CLAUDE = FIXTURES_DIR / "fake_claude.py"
FAKE_CODEX = FIXTURES_DIR / "fake_codex.py"


def init_repo_with_config(tmp_path: Path, *, blocking_severities: list[str] | None = None) -> Path:
    """A real Git repository with a committed, schema-valid
    `.ai-workflow.toml` + `.gitignore` on `main` - the common starting
    point every lifecycle test builds on."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)

    exit_code = main(
        ["init", "--project-name", "lifecycle-test-project", "--stack", "other", "--project-root", str(tmp_path)]
    )
    assert exit_code == 0, "init failed while setting up a lifecycle test fixture"

    if blocking_severities is not None:
        config_path = tmp_path / ".ai-workflow.toml"
        content = config_path.read_text(encoding="utf-8")
        content = content.replace(
            'blocking_severities = ["blocker", "major"]',
            "blocking_severities = " + json.dumps(blocking_severities),
        )
        config_path.write_text(content, encoding="utf-8")

    subprocess.run(["git", "-C", str(tmp_path), "add", ".ai-workflow.toml", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "bootstrap workflow config"], check=True)
    return tmp_path


def write_spec_kit_tasks(project_root: Path, *, spec_dir: str = "specs", feature: str = "001-demo", tasks: dict[str, str]) -> None:
    """A minimal `tasks.md` fixture, in Spec Kit's own `- [ ] T### <title>`
    line shape (`speckit/integration.py`'s own parser), under a numbered
    feature directory - matching this repository's own on-disk convention
    and `cli.py`'s `_resolve_tasks_md_path` heuristic."""
    feature_dir = project_root / spec_dir / feature
    feature_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"- [ ] {task_id} {title}" for task_id, title in tasks.items()]
    (feature_dir / "tasks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def claude_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """The env-var *overlay* `run_cli` applies on top of a cleared slate
    (never a full `os.environ` snapshot - see `run_cli`'s own docstring
    for why that would let one call's `FAKE_*` variable leak into the
    next), pointing `run-claude` at the fake `claude` CLI stub with
    `overrides` controlling its behavior (`FAKE_CLAUDE_STATUS`,
    `FAKE_CLAUDE_TRANSIENT_FAILURE`, ...)."""
    env = {"SOLARI_WORKFLOW_TEST_CLAUDE_ARGV_PREFIX": json.dumps([sys.executable, str(FAKE_CLAUDE)])}
    env.update(overrides or {})
    return env


def codex_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """The `run-codex-gate` counterpart to :func:`claude_env`."""
    env = {"SOLARI_WORKFLOW_TEST_CODEX_ARGV_PREFIX": json.dumps([sys.executable, str(FAKE_CODEX)])}
    env.update(overrides or {})
    return env


# Every environment variable a fake-stub-driven CLI call might set, so a
# PRIOR call's `monkeypatch.setenv` within the same test can never leak
# into a LATER call that doesn't re-specify it (`monkeypatch.setenv`
# persists until the test's own teardown, not just for one `main(...)` call).
_KNOWN_FAKE_ACTOR_ENV_VARS = (
    "SOLARI_WORKFLOW_TEST_CLAUDE_ARGV_PREFIX",
    "SOLARI_WORKFLOW_TEST_CODEX_ARGV_PREFIX",
    "FAKE_CLAUDE_STATUS",
    "FAKE_CLAUDE_EXIT_CODE",
    "FAKE_CLAUDE_TRANSIENT_FAILURE",
    "FAKE_CLAUDE_TRANSCRIPT",
    "FAKE_CODEX_RESULT",
    "FAKE_CODEX_FINDINGS",
    "FAKE_CODEX_EXIT_CODE",
    "FAKE_CODEX_TRANSIENT_FAILURE",
)


def run_cli(args: list[str], *, env: dict[str, str] | None = None, monkeypatch=None) -> int:
    """Invoke the real CLI entry point in-process. `env` (typically built
    via :func:`claude_env`/:func:`codex_env`) is applied via `monkeypatch`
    against the REAL `os.environ` - the actor modules launch the fake
    stubs with `env=None` (inherit), so this is what those subprocesses
    actually see.

    Every known `FAKE_*`/argv-prefix variable is unconditionally cleared
    FIRST, regardless of what `env` contains, so a previous call's
    variable (e.g. `FAKE_CODEX_FINDINGS` from an earlier gate in the same
    test) can never silently survive into a call that does not
    re-specify it - `monkeypatch.setenv`/`delenv` persist until the
    test's own teardown, not just for one `main(...)` call.
    """
    if env is None:
        return main(args)
    assert monkeypatch is not None, "env overrides require the caller's monkeypatch fixture"
    for var in _KNOWN_FAKE_ACTOR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return main(args)
