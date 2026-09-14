"""PascalCase block-name derivation and branch/tag naming (research.md §16).

The single normalization routine here (:func:`normalize_words`) is also
reused, unmodified, by `.ai-runs/` slug derivation (research.md §8) via
:func:`to_slug` — see `runs/audit.py`.

Task IDs are written with a leading ``T`` in `tasks.md` and on the CLI
(e.g. ``T091``), but the branch/tag templates in data-model.md and
research.md §16 (``T{first}-{PascalCase}``,
``checkpoint-T{first}-T{last}``) already supply that ``T`` themselves —
:func:`task_id_number` strips a leading ``T``/``t`` so a caller can pass
either ``"T091"`` or ``"091"`` and get the same, single-``T`` result
(``T091-...``, never ``TT091-...``).
"""

from __future__ import annotations

import re
import unicodedata

_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_VALID_PASCAL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_BRANCH_RE = re.compile(r"^T(?P<first>\d+)-(?P<pascal>[A-Za-z][A-Za-z0-9]*)$")
_CHECKPOINT_TAG_RE = re.compile(r"^checkpoint-T(?P<first>\d+)-T(?P<last>\d+)$")

# Fourth remediation (B3): strict block-name grammar. ASCII letters and
# digits, with single inner separators from {space, "-", "_", "."}; starts
# and ends with a letter or digit; at most 80 characters; must contain a
# letter (so the PascalCase branch segment is never the fallback). No line
# breaks, control characters, tabs, ":" or any other character that could
# become an extra line or a forged field in a Git message.
BLOCK_NAME_MAX_LENGTH = 80
_BLOCK_NAME_RE = re.compile(r"[A-Za-z0-9]+(?:[ ._-][A-Za-z0-9]+)*")
_TASK_ID_RE = re.compile(r"[Tt]?[0-9]+")


def task_id_number(task_id: str) -> str:
    """Normalize a task id like `"T091"` or `"091"` to its bare numeric suffix."""
    if task_id and task_id[0] in ("T", "t"):
        return task_id[1:]
    return task_id


def normalize_words(name: str) -> list[str]:
    """Shared normalization pipeline: NFKD-normalize, strip combining marks,
    then split on any run of non-alphanumeric characters.

    Transliterates accented Latin text to its ASCII base (e.g. `"café"` →
    `["cafe"]`); a non-Latin script with no ASCII decomposition (e.g.
    Cyrillic, Japanese) normalizes away to nothing, yielding no words at
    all — the caller's fallback path is what handles that case.
    """
    normalized = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return [word for word in _WORD_SPLIT_RE.split(stripped) if word]


def _case_word(word: str) -> str:
    if word.isdigit():
        return word
    if word.isupper() and len(word) >= 2:
        return word  # preserved acronym, e.g. "API", "CI"
    return word[0].upper() + word[1:].lower()


def to_pascal_case(name: str, *, fallback_task_id: str) -> str:
    """Deterministically derive a PascalCase block-name segment (research.md §16).

    Falls back to `Block{fallback_task_id}` when the input has no
    ASCII-representable content after normalization, or the result isn't
    a valid identifier-shaped string — always valid, always traceable via
    the task ID already present in the branch-name prefix.
    """
    words = normalize_words(name)
    candidate = "".join(_case_word(word) for word in words)
    if not candidate or not _VALID_PASCAL_RE.match(candidate):
        return f"Block{fallback_task_id}"
    return candidate


def to_slug(name: str) -> str:
    """Lower-cased, hyphen-joined slug sharing the PascalCase normalization pipeline."""
    return "-".join(word.lower() for word in normalize_words(name))


def validate_block_name(block_name: str) -> str:
    """Return `block_name` unchanged if it satisfies the strict grammar,
    else raise `BlockIdentityInputError` (fourth remediation, B3)."""
    from solari_workflow.errors import BlockIdentityInputError

    if (
        not isinstance(block_name, str)
        or len(block_name) > BLOCK_NAME_MAX_LENGTH
        or _BLOCK_NAME_RE.fullmatch(block_name) is None
        or not any(ch.isalpha() for ch in block_name)
    ):
        raise BlockIdentityInputError(
            f"invalid block name {block_name!r}: use 1-{BLOCK_NAME_MAX_LENGTH} ASCII letters/digits "
            "separated by single spaces, '-', '_' or '.', starting and ending with a letter or digit "
            "and containing at least one letter (no line breaks, control characters or ':')"
        )
    return block_name


def validate_task_id(task_id: str) -> str:
    """Return `task_id` unchanged if it is exactly `T<ASCII digits>` (or
    bare digits), else raise `BlockIdentityInputError` - `int()` alone
    would accept surrounding whitespace/newlines (fourth remediation, B3)."""
    from solari_workflow.errors import BlockIdentityInputError

    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise BlockIdentityInputError(f"invalid task ID {task_id!r}: expected 'T' followed by ASCII digits")
    return task_id


def build_branch_name(first_task_id: str, block_name: str) -> str:
    """`T{first_task_id}-{PascalCase(block_name)}` (data-model.md's Development Block)."""
    number = task_id_number(first_task_id)
    pascal = to_pascal_case(block_name, fallback_task_id=number)
    return f"T{number}-{pascal}"


def parse_branch_name(branch_name: str) -> tuple[str, str] | None:
    """Return `(first_task_number, pascal_block_name)`, or `None` if it doesn't match."""
    match = _BRANCH_RE.match(branch_name)
    if not match:
        return None
    return match.group("first"), match.group("pascal")


def build_checkpoint_tag_name(first_task_id: str, last_task_id: str) -> str:
    """`checkpoint-T{first}-T{last}` (research.md §14)."""
    first = task_id_number(first_task_id)
    last = task_id_number(last_task_id)
    return f"checkpoint-T{first}-T{last}"


def parse_checkpoint_tag_name(tag_name: str) -> tuple[str, str] | None:
    """Return `(first_task_number, last_task_number)`, or `None` if it doesn't match."""
    match = _CHECKPOINT_TAG_RE.match(tag_name)
    if not match:
        return None
    return match.group("first"), match.group("last")
