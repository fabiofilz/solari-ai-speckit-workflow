"""Read-only Spec Kit artifact discovery and task-title lookup.

This module only *reads* what Spec Kit's own CLI/scripts already wrote —
it never reimplements Spec Kit's own feature-directory resolution,
`/speckit.*` commands, or scripts (research.md §0.1's Spec Kit row: "does
not choose block boundaries or Git topology... only supplies the task
list the Orchestrator selects a range from"). Callers pass the concrete
Spec Kit artifact directory to inspect (e.g. `specs/001-workflow-v1`);
this module does not itself decide which feature directory is "current".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from solari_workflow.git.branch import task_id_number

_TASK_LINE_RE = re.compile(
    r"^-\s*\[[ xX]\]\s*(?P<id>T\d+)\s*(?:\[P\]\s*)?(?:\[[A-Za-z0-9]+\]\s*)*(?P<title>.+?)\s*$"
)


@dataclass(frozen=True)
class SpecKitArtifacts:
    """Presence of the four Spec Kit artifacts research.md/data-model.md call out.

    "Clarification" is not a separate file — Spec Kit's `/clarify`
    command edits a `## Clarifications` section inside `spec.md` itself —
    so `clarification_present` is true only when `spec.md` exists *and*
    that section has actual content, not merely a bare heading.
    """

    spec_dir: Path
    specification_present: bool
    clarification_present: bool
    plan_present: bool
    tasks_present: bool


_CLARIFICATIONS_HEADING_RE = re.compile(r"^##\s+Clarifications\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)


def _has_clarifications_content(spec_path: Path) -> bool:
    if not spec_path.is_file():
        return False
    text = spec_path.read_text(encoding="utf-8")
    heading_match = _CLARIFICATIONS_HEADING_RE.search(text)
    if heading_match is None:
        return False
    section_start = heading_match.end()
    next_heading_match = _NEXT_HEADING_RE.search(text, pos=section_start)
    section_end = next_heading_match.start() if next_heading_match else len(text)
    section_body = text[section_start:section_end].strip()
    return bool(section_body)


def discover_artifacts(spec_dir: Path) -> SpecKitArtifacts:
    """Inspect `spec_dir` for the four Spec Kit artifacts, read-only.

    `spec_dir` is the specific feature directory to inspect (e.g.
    `specs/001-workflow-v1`), already resolved by the caller — this
    function performs no directory discovery of its own.
    """
    spec_path = spec_dir / "spec.md"
    return SpecKitArtifacts(
        spec_dir=spec_dir,
        specification_present=spec_path.is_file(),
        clarification_present=_has_clarifications_content(spec_path),
        plan_present=(spec_dir / "plan.md").is_file(),
        tasks_present=(spec_dir / "tasks.md").is_file(),
    )


def parse_task_titles(tasks_md_path: Path) -> dict[str, str]:
    """Parse every `- [ ] T### ... <title>` line in `tasks.md` into `{task_id: title}`.

    A read-only lookup used by checkpoint tag metadata (research.md §14)
    — never a rerun of anything, and never Spec Kit's own task-generation
    logic.
    """
    if not tasks_md_path.is_file():
        return {}
    titles: dict[str, str] = {}
    for line in tasks_md_path.read_text(encoding="utf-8").splitlines():
        match = _TASK_LINE_RE.match(line.strip())
        if match:
            titles[match.group("id")] = match.group("title").strip()
    return titles


def lookup_task_titles(
    tasks_md_path: Path, first_task_id: str, last_task_id: str
) -> dict[str, str | None]:
    """`{task_id: title}` for every id in the inclusive range `[first, last]`.

    A task ID with no corresponding line in `tasks.md` (a gap, or a
    requested id outside the file's own range) maps to `None` rather
    than being silently omitted, so a caller can tell "no title found"
    apart from "not requested".
    """
    all_titles = parse_task_titles(tasks_md_path)
    first_n = int(task_id_number(first_task_id))
    last_n = int(task_id_number(last_task_id))
    width = max(len(task_id_number(first_task_id)), len(task_id_number(last_task_id)))

    result: dict[str, str | None] = {}
    for number in range(first_n, last_n + 1):
        task_id = f"T{number:0{width}d}"
        result[task_id] = all_titles.get(task_id)
    return result
