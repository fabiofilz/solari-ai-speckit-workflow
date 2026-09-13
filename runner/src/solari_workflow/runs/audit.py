"""`.ai-runs/` prompt/result audit trail (research.md §8) — append-only, immutable.

Naming convention: `YYYYMMDD-NNN-slug-{prompt,result}.md`. `NNN` is the
max sequence number already used *for that UTC date*, plus one. The
prompt file is created with `os.open(..., O_CREAT | O_EXCL)` as a final
safety net against an unexpected collision (e.g. a manually placed file
with the same name) — on collision, the runner retries with `NNN+1`
(bounded) rather than ever overwriting (FR-024/SC-007). The module never
reopens an existing pair for editing — every execution or re-gate
creates a brand-new numbered pair.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from solari_workflow.git.branch import to_slug

_STEM_RE = re.compile(r"^(?P<date>\d{8})-(?P<seq>\d{3})-(?P<slug>.+)-(?P<kind>prompt|result)\.md$")
_MAX_COLLISION_RETRIES = 20


class AuditTrailError(Exception):
    """Raised when a new run-record pair cannot be created after bounded retries."""


class ResultAlreadyRecordedError(Exception):
    """Raised when `write_result` is called against a pair whose result file already exists.

    The audit trail is immutable — there is no "edit" API; a repeated
    call for the same record is rejected rather than silently
    overwriting the existing, permanent record.
    """


@dataclass(frozen=True)
class RunRecordPaths:
    """The paired prompt/result file paths for one execution or gate."""

    prompt_path: Path
    result_path: Path
    stem: str


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_sequence(ai_runs_dir: Path, date: str) -> int:
    max_seq = 0
    if ai_runs_dir.is_dir():
        for entry in ai_runs_dir.iterdir():
            match = _STEM_RE.match(entry.name)
            if match and match.group("date") == date:
                max_seq = max(max_seq, int(match.group("seq")))
    return max_seq + 1


def _create_exclusive(path: Path, content: str) -> None:
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def create_run_record(ai_runs_dir: Path, *, slug_source: str, prompt_content: str) -> RunRecordPaths:
    """Create a brand-new, immutable `.ai-runs/*-prompt.md` file.

    Returns the paths for both halves of the pair; the `*-result.md`
    file itself is not created here — call :func:`write_result` once the
    outcome is known.
    """
    ai_runs_dir.mkdir(parents=True, exist_ok=True)
    date = _today_utc()
    slug = to_slug(slug_source) or "run"
    sequence = _next_sequence(ai_runs_dir, date)

    last_error: OSError | None = None
    for attempt in range(_MAX_COLLISION_RETRIES):
        seq = sequence + attempt
        stem = f"{date}-{seq:03d}-{slug}"
        prompt_path = ai_runs_dir / f"{stem}-prompt.md"
        result_path = ai_runs_dir / f"{stem}-result.md"
        try:
            _create_exclusive(prompt_path, prompt_content)
        except FileExistsError as exc:
            last_error = exc
            continue
        return RunRecordPaths(prompt_path=prompt_path, result_path=result_path, stem=stem)

    raise AuditTrailError(
        f"could not create a run-record pair under {ai_runs_dir} after "
        f"{_MAX_COLLISION_RETRIES} attempts (persistent naming collisions)"
    ) from last_error


def write_result(record: RunRecordPaths, content: str) -> Path:
    """Create the paired, immutable result file.

    Raises :class:`ResultAlreadyRecordedError` if it already exists — the
    module never reopens an existing record for editing.
    """
    try:
        _create_exclusive(record.result_path, content)
    except FileExistsError as exc:
        raise ResultAlreadyRecordedError(
            f"result already recorded at {record.result_path}; the audit trail is immutable"
        ) from exc
    return record.result_path


def render_metadata_header(
    *,
    tool: str,
    model: str | None,
    effort: str | None,
    session_mode: str | None,
    prior_session_id: str | None,
    speckit_involved: bool,
    scope: str,
    purpose: str,
    workflow_schema_version: str,
    workflow_version: str,
    paired_prompt_stem: str | None = None,
) -> str:
    """Plain-Markdown metadata header shared by prompt and result files (research.md §8).

    research.md §8 requires "workflow schema/version" in every header —
    `[workflow].schema_version` (the fixed v1 schema gate, e.g. `"1"`)
    and `[workflow].version` (`data-model.md`'s separate free-form
    version field) are two distinct config fields, so both get their own
    line here; a header naming only the schema version was incomplete.

    `paired_prompt_stem` renders the result record's required "explicit
    pointer back to its paired prompt file by shared `YYYYMMDD-NNN-slug`
    stem" (research.md §8) — pass it only when rendering a *result*
    file's header; a prompt file's own header has nothing to point to
    and omits the line entirely.

    Never a raw environment-variable *value* — only variable *names*, if
    referenced at all; callers are responsible for not passing secrets
    into `purpose`/`scope`.
    """
    session_mode_line = f"- Session Mode: {session_mode or 'n/a'}"
    if session_mode == "CONTINUE" and prior_session_id:
        session_mode_line += f" (prior session: {prior_session_id})"

    lines = [
        f"- Tool: {tool}",
        f"- Model: {model or 'n/a'}",
        f"- Effort: {effort or 'n/a'}",
        session_mode_line,
        f"- Spec Kit Involved: {speckit_involved}",
        f"- Scope: {scope}",
        f"- Purpose: {purpose}",
        f"- Timestamp: {_timestamp_utc()}",
        f"- Workflow Schema Version: {workflow_schema_version}",
        f"- Workflow Version: {workflow_version}",
    ]
    if paired_prompt_stem is not None:
        lines.append(f"- Paired Prompt: {paired_prompt_stem}-prompt.md")
    return "\n".join(lines) + "\n"
