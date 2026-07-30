"""Contract: the built SPA route manifest mirrors web_frontend/src/routes/ +page paths."""

from __future__ import annotations

from pathlib import Path

from scripts.check_web_hub_spa_shell import (
    expected_route_templates,
    routes_dir,
    run_checks,
)


def test_expected_route_templates_matches_repo_routes() -> None:
    root = Path(__file__).resolve().parents[3]
    templates = expected_route_templates(routes_dir(root))
    assert "/chats" in templates
    assert "/chats/{chatId}" in templates
    assert "/repos/{repoId}/contextspace" in templates
    assert "/hub" in templates
    assert "/services" in templates
    assert "/worktrees" in templates
    assert "/contextspace/{workspaceId}" in templates


def test_web_spa_shell_checker_passes_on_repo_tree() -> None:
    """Requires a fresh `pnpm web:build` so web_static/spa_routes.json is in sync."""
    root = Path(__file__).resolve().parents[3]
    errors, _templates = run_checks(repo_root=root)
    assert not errors, errors
