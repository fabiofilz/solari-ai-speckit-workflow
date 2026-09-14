"""Regression tests for M1 (the structured gate audit record - both
prompt and result halves - must carry Fingerprint X's full identity, not
merely `.block-state.json`'s mutable cache) - and that two different
gate attempts for the same block produce two audit records with two
DIFFERENT recorded Fingerprints, never a value that could be confused
between them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from _lifecycle_helpers import claude_env, codex_env, init_repo_with_config, run_cli, write_spec_kit_tasks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


def _gate_records(ai_runs_dir: Path) -> list[tuple[Path, Path]]:
    prompts = sorted(ai_runs_dir.glob("*-run-codex-gate-prompt.md"))
    pairs = []
    for prompt in prompts:
        result = ai_runs_dir / prompt.name.replace("-prompt.md", "-result.md")
        pairs.append((prompt, result))
    return pairs


def _gate_identity_block(text: str) -> dict:
    """Parse the canonical-JSON `Gate Identity:` block (third remediation,
    M3) out of a gate audit half."""
    marker = "Gate Identity:\n```json\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    return json.loads(text[start:end])


def test_gate_prompt_and_result_both_carry_fingerprint_x_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0
    (project_root / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0

    ai_runs_dir = project_root / ".ai-runs"
    records = _gate_records(ai_runs_dir)
    assert len(records) == 1
    prompt_path, result_path = records[0]
    assert result_path.is_file()

    prompt_text = prompt_path.read_text(encoding="utf-8")
    result_text = result_path.read_text(encoding="utf-8")
    identities = [_gate_identity_block(text) for text in (prompt_text, result_text)]
    assert identities[0] == identities[1]
    identity = identities[0]
    fingerprint = identity["fingerprint"]
    assert len(fingerprint["head_oid"]) == 40
    assert len(fingerprint["candidate_tree_oid"]) == 40
    assert fingerprint["branch_name"] == "T091-ValidationPipeline"
    assert fingerprint["first_task"] == "T091"
    assert fingerprint["last_task"] == "T091"
    assert identity["feature_identity"] == "specs/001-demo/tasks.md"
    assert identity["gated_main_oid"] == fingerprint["head_oid"]

    # The immutable structured evidence carries the SAME identity.
    evidence_files = sorted(ai_runs_dir.glob("*-run-codex-gate-gate-identity.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["identity"] == identity
    assert evidence["result"] == "PASS"
    assert evidence["checkpoint_eligible"] is True


def test_two_gate_attempts_record_two_distinct_fingerprints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A FAIL followed by a remediation and a fresh PASS must leave two
    audit records behind, each carrying a DIFFERENT `candidate_tree_oid`
    - the second gate's PASS record is never confusable with the first's
    superseded (and, per B1, revoked) attempt."""
    project_root = init_repo_with_config(tmp_path)
    write_spec_kit_tasks(project_root, tasks={"T091": "Add validation pipeline"})
    assert run_cli(
        [
            "start-block", "--first-task", "T091", "--last-task", "T091",
            "--block-name", "Validation Pipeline", "--project-root", str(project_root),
        ]
    ) == 0
    (project_root / "feature.py").write_text("def feature():\n    return None\n", encoding="utf-8")
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "implement", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "FAIL", "FAKE_CODEX_FINDINGS": "blocker:bug"}),
        monkeypatch=monkeypatch,
    ) == 1

    (project_root / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")
    assert run_cli(
        [
            "run-claude", "--model", "m", "--effort", "high", "--session-mode", "NEW",
            "--purpose", "fix", "--project-root", str(project_root),
        ],
        env=claude_env({"FAKE_CLAUDE_STATUS": "COMPLETE"}),
        monkeypatch=monkeypatch,
    ) == 0
    assert run_cli(
        ["run-codex-gate", "--project-root", str(project_root)],
        env=codex_env({"FAKE_CODEX_RESULT": "PASS"}),
        monkeypatch=monkeypatch,
    ) == 0

    ai_runs_dir = project_root / ".ai-runs"
    records = _gate_records(ai_runs_dir)
    assert len(records) == 2

    oids = [
        _gate_identity_block(result_path.read_text(encoding="utf-8"))["fingerprint"]["candidate_tree_oid"]
        for _, result_path in records
    ]
    assert oids[0] != oids[1]
