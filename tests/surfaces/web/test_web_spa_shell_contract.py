"""Contract: derived SvelteKit +page URLs get Web HTML shell (or legacy redirect)."""

from __future__ import annotations

from pathlib import Path

from scripts.check_web_hub_spa_shell import collect_probe_paths, routes_dir, run_checks


def test_collect_probe_paths_matches_repo_routes() -> None:
    root = Path(__file__).resolve().parents[3]
    paths = collect_probe_paths(routes_dir(root))
    assert "/chats" in paths
    assert "/chats/00000000-0000-4000-8000-000000000001" not in paths
    assert "/repos/probe-repo/contextspace" in paths
    assert "/hub" in paths
    assert "/worktrees" in paths
    assert "/contextspace/probe-workspace" in paths


def test_web_spa_shell_checker_passes_on_repo_tree() -> None:
    root = Path(__file__).resolve().parents[3]
    errors, _paths = run_checks(repo_root=root)
    assert not errors, errors
