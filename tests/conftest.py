"""Test harness configuration.

This repo uses a `src/` layout. In some developer environments an older
installed `codex_autorunner` package can shadow the local sources.

Ensure tests always import the in-repo code.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
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
def hub_env(tmp_path: Path):
    """Create a minimal hub with a single initialized repo mounted under `/repos/<id>`."""

    # Import lazily so `pytest_configure()` can prepend the local src/ directory
    # before any `codex_autorunner` modules are loaded.
    from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
    from codex_autorunner.core.config import load_hub_config
    from codex_autorunner.manifest import load_manifest, save_manifest

    @dataclass(frozen=True)
    class HubEnv:
        hub_root: Path
        repo_id: str
        repo_root: Path

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    # Put the repo under the hub's default repos_root (worktrees/ by default).
    repo_id = "repo"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)

    return HubEnv(hub_root=hub_root, repo_id=repo_id, repo_root=repo_root)


@pytest.fixture()
def repo(hub_env) -> Path:
    """Backwards-compatible repo fixture (the hub's single test repo root)."""
    return hub_env.repo_root
