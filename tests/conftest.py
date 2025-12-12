"""Test harness configuration.

This repo uses a `src/` layout. In some developer environments an older
installed `codex_autorunner` package can shadow the local sources.

Ensure tests always import the in-repo code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def pytest_configure() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    src_path = str(src_dir)
    if sys.path[:1] != [src_path] and src_path not in sys.path:
        sys.path.insert(0, src_path)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """
    Create a minimal initialized repo on disk.

    Several tests rely on `create_app(repo_root)` which requires:
    - a `.git/` directory
    - `.codex-autorunner/config.yml` (and work docs) to exist
    """

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    # Import lazily so `pytest_configure()` can prepend the local src/ directory
    # before any `codex_autorunner` modules are loaded.
    from codex_autorunner.bootstrap import seed_repo_files

    seed_repo_files(repo_root, git_required=False)
    return repo_root
