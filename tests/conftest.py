"""Test harness configuration.

This repo uses a `src/` layout. In some developer environments an older
installed `codex_autorunner` package can shadow the local sources.

Ensure tests always import the in-repo code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

DEFAULT_NON_INTEGRATION_TIMEOUT_SECONDS = 120


def pytest_configure() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    src_path = str(src_dir)
    if sys.path[:1] != [src_path] and src_path not in sys.path:
        sys.path.insert(0, src_path)


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """
    Apply a default per-test timeout to non-integration tests.

    This relies on `pytest-timeout` when installed; if it isn't installed, the
    marker is inert but still documents the intent.
    """
    _ = session, config
    for item in items:
        if item.get_closest_marker("integration") is not None:
            continue
        item.add_marker(pytest.mark.timeout(DEFAULT_NON_INTEGRATION_TIMEOUT_SECONDS))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """
    Create a minimal initialized repo on disk.

    Several tests rely on `create_app(repo_root)` which requires:
    - a `.git/` directory
    - a hub config at `.codex-autorunner/config.yml`
    - work docs/state to exist
    """

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    # Import lazily so `pytest_configure()` can prepend the local src/ directory
    # before any `codex_autorunner` modules are loaded.
    from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files

    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)
    return repo_root
