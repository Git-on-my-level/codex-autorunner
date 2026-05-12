#!/usr/bin/env python3
"""Verify hub GET routes serve the PMA HTML shell for every SvelteKit +page path.

SvelteKit navigates in-browser without round-tripping the server; a full load
(refresh / open in new tab) must receive index.html for each URL the client can
render, or FastAPI returns JSON and the tab appears blank.

This script builds probe URLs from directory names under
``web_frontend/src/routes`` (same rules as SvelteKit dynamic segments) and
asserts each probe succeeds with either:
  - 200 + Web Hub HTML markers, or
  - 307/308 (legacy redirects for /worktrees).

Usage:
  python scripts/check_web_hub_spa_shell.py

Exit code 0 on success, 1 on failure, 2 on environment/setup errors.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Param names in brackets map to stable probe slugs seeded in the temp manifest.
PROBE_BY_PARAM: dict[str, str] = {
    "repoid": "probe-repo",
    "worktreeid": "probe-worktree",
    "ticketid": "TICKET-SHELL-CHECK",
    "workspaceid": "probe-workspace",
    "chatid": "00000000-0000-4000-8000-000000000001",
}

# Prefixes where the hub returns redirects instead of the HTML shell.
REDIRECT_PREFIXES: tuple[str, ...] = ("/worktrees",)

_WEB_MARKERS: tuple[str, ...] = (
    "<title>Web Hub</title>",
    "/_app/immutable/entry/app.",
)


def _repo_root() -> Path:
    return REPO_ROOT


def routes_dir(repo_root: Path) -> Path:
    return repo_root / "src" / "codex_autorunner" / "web_frontend" / "src" / "routes"


def segment_to_probe_slug(segment: str) -> str:
    """Map one SvelteKit route directory name to a URL path segment."""
    m = re.fullmatch(r"\[\[([^\]]+)\]\]", segment)
    if m:
        inner = m.group(1).strip().lower()
        return PROBE_BY_PARAM.get(inner, f"probe-{inner}")
    m = re.fullmatch(r"\[([^\]]+)\]", segment)
    if m:
        inner = m.group(1).strip().lower()
        return PROBE_BY_PARAM.get(inner, f"probe-{inner}")
    return segment


def _route_segment_probe_variants(rel_parent: Path) -> list[list[str]]:
    """Path segment lists for each +page URL variant (e.g. optional ``[[param]]`` omitted)."""
    raw_parts = list(rel_parent.parts)
    non_group = [p for p in raw_parts if not (p.startswith("(") and p.endswith(")"))]
    if not non_group:
        return []
    slug_parts = [segment_to_probe_slug(p) for p in non_group]
    opt_run = 0
    for p in reversed(non_group):
        if re.fullmatch(r"\[\[([^\]]+)\]\]", p):
            opt_run += 1
        else:
            break
    if opt_run == 0:
        return [slug_parts]
    variants: list[list[str]] = []
    for drop in range(0, opt_run + 1):
        variant = slug_parts[: len(slug_parts) - drop]
        if variant:
            variants.append(variant)
    return variants


def collect_probe_paths(routes: Path) -> list[str]:
    """Return sorted unique URL paths (e.g. /chats/uuid) for every +page.svelte."""
    if not routes.is_dir():
        raise FileNotFoundError(f"PMA routes directory missing: {routes}")

    urls: set[str] = set()
    for page in sorted(routes.rglob("+page.svelte")):
        if "node_modules" in page.parts:
            continue
        rel_parent = page.parent.relative_to(routes)
        if rel_parent == Path("."):
            # Root layout page; hub serves / with a redirect — checked separately.
            continue
        for parts in _route_segment_probe_variants(rel_parent):
            urls.add("/" + "/".join(parts))

    # /hub and / are not always inferred the way we want; force-check.
    urls.add("/hub")
    return sorted(urls)


def _expects_redirect(path: str) -> bool:
    if path in REDIRECT_PREFIXES:
        return True
    return any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in REDIRECT_PREFIXES
    )


def _prepare_hub(hub_root: Path) -> None:
    from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
    from codex_autorunner.core.config import load_hub_config
    from codex_autorunner.manifest import load_manifest, save_manifest

    seed_hub_files(hub_root, force=True)
    base_id = PROBE_BY_PARAM["repoid"]
    worktree_id = PROBE_BY_PARAM["worktreeid"]
    base_root = hub_root / base_id
    worktree_root = hub_root / "worktrees" / worktree_id
    seed_repo_files(base_root, force=True, git_required=False)
    seed_repo_files(worktree_root, force=True, git_required=False)
    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(
        hub_root,
        base_root,
        repo_id=base_id,
        kind="base",
    )
    manifest.ensure_repo(
        hub_root,
        worktree_root,
        repo_id=worktree_id,
        kind="worktree",
        worktree_of=base_id,
        branch="feature/test",
    )
    save_manifest(hub_config.manifest_path, manifest, hub_root)


def run_checks(*, repo_root: Path) -> tuple[list[str], list[str]]:
    """Return (errors, probe_paths)."""
    errors: list[str] = []
    rd = routes_dir(repo_root)
    try:
        paths = collect_probe_paths(rd)
    except FileNotFoundError as exc:
        return [str(exc)], []

    sys.path.insert(0, str(repo_root / "src"))
    from fastapi.testclient import TestClient

    from codex_autorunner.server import create_hub_app

    with tempfile.TemporaryDirectory() as tmp:
        hub_root = Path(tmp) / "hub"
        _prepare_hub(hub_root)
        client = TestClient(create_hub_app(hub_root))

        for path in paths:
            response = client.get(path, follow_redirects=False)
            if _expects_redirect(path):
                if response.status_code not in (307, 308):
                    errors.append(
                        f"{path}: expected redirect (307/308), got {response.status_code}"
                    )
                continue
            if response.status_code != 200:
                errors.append(
                    f"{path}: expected 200 HTML shell, got {response.status_code}: "
                    f"{response.text[:200]!r}"
                )
                continue
            body = response.text
            missing = [m for m in _WEB_MARKERS if m not in body]
            if missing:
                errors.append(
                    f"{path}: PMA HTML markers missing {missing}; "
                    f"body prefix: {body[:160]!r}"
                )
    return errors, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_repo_root(),
        help="Repository root (default: parent of scripts/)",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    errors, paths = run_checks(repo_root=repo_root)
    if errors:
        print("Web Hub SPA shell check failed:", file=sys.stderr)
        for line in errors:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nFix: add matching @app.get routes that return _web_index_response() "
            "(see surfaces/web/app.py, comment “Web Hub SPA shell”).",
            file=sys.stderr,
        )
        return 1
    print(f"Web Hub SPA shell check OK ({repo_root}) — {len(paths)} paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
