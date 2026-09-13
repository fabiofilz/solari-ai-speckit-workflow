"""Smoke test proving the solari_workflow package bootstraps correctly."""

import solari_workflow


def test_package_imports() -> None:
    """Importing the installed package must succeed and resolve as a package."""
    assert solari_workflow.__name__ == "solari_workflow"
    assert hasattr(solari_workflow, "__path__")
