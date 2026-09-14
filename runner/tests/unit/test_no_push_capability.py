"""T049: the manual-only push policy as an explicit ABSENCE
(`[git.push].mode = "manual"`, research.md §0.2/FR-031) - `cli.py` exposes
no `push` subcommand, and no module under `git/` ever constructs a `git
push` argv. A static-scan guard against a future accidental addition,
not merely a behavioral test of what exists today.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from solari_workflow.cli import _build_parser

GIT_PACKAGE_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "solari_workflow" / "git"


def test_cli_exposes_no_push_subcommand() -> None:
    parser = _build_parser()
    subparsers_action = next(
        action for action in parser._actions if action.dest == "command"  # noqa: SLF001 - argparse introspection
    )
    assert "push" not in subparsers_action.choices


@pytest.mark.parametrize("path", sorted(GIT_PACKAGE_DIR.glob("*.py")))
def test_no_module_under_git_ever_constructs_a_git_push_argv(path: Path) -> None:
    """Parses each `git/*.py` module's AST and asserts no string literal
    anywhere in it is exactly `"push"` used as an element of a list/tuple
    literal (the shape every `argv` this codebase builds takes,
    `git/ops.py`'s own `_run(["diff", ...])`/`self._run(["branch", ...])`
    convention) - a static guard that survives even if the module is
    never actually executed by the rest of the test suite.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            literal_strings = [elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
            assert "push" not in literal_strings, f"{path}: found a 'push' element in an argv-shaped literal: {literal_strings}"


def test_git_ops_has_no_push_method() -> None:
    from solari_workflow.git import ops

    assert not hasattr(ops.GitRepo, "push")
    assert not any("push" in name.lower() for name in dir(ops.GitRepo) if not name.startswith("_"))
