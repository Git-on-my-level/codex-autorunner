#!/usr/bin/env python3
"""Verify the built Web Hub SPA route manifest is present and in sync.

The hub used to hand-maintain a second, parallel list of FastAPI routes
mirroring ``web_frontend/src/routes/**/+page.svelte`` so that a full page
load (refresh / open in new tab) of any SvelteKit-routable URL gets
``index.html`` instead of FastAPI's JSON 404. That hand-maintained list and
the frontend's routes could (and did) drift.

The frontend build now materializes the filesystem router as
``web_static/spa_routes.json`` (see
``web_frontend/scripts/generate-spa-routes.mjs``), and
``src/codex_autorunner/surfaces/web/app.py`` reads that manifest at app
construction time instead of hand-mirroring the route list. This script no
longer boots the hub app or walks routes over HTTP -- that runtime
correctness proof lives in
``tests/surfaces/web/test_web_static_routes.py``. Its only remaining job is a
fast, no-server-boot build-freshness check: recompute the expected route
templates directly from ``web_frontend/src/routes/`` and confirm the shipped
manifest matches, i.e. that ``pnpm web:build`` actually ran after the most
recent routing change.

Usage:
  python scripts/check_web_hub_spa_shell.py

Exit code 0 on success, 1 on drift (manifest present but stale), 2 on
environment/setup errors (manifest or routes directory missing).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MANIFEST_FILENAME = "spa_routes.json"

# Kept for reference/consistency with app.py; this script only verifies the
# manifest is a faithful, current mirror of the filesystem -- it doesn't
# apply hub-side routing policy (nest-tolerant segments, worktree scoping),
# since that policy is intentionally NOT derived from the manifest.
_OPTIONAL_SEGMENT = re.compile(r"^\[\[([^\]]+)\]\]$")
_REST_SEGMENT = re.compile(r"^\[\.\.\.([^\]]+)\]$")
_DYNAMIC_SEGMENT = re.compile(r"^\[([^\]]+)\]$")


def _repo_root() -> Path:
    return REPO_ROOT


def routes_dir(repo_root: Path) -> Path:
    return repo_root / "src" / "codex_autorunner" / "web_frontend" / "src" / "routes"


def manifest_path(repo_root: Path) -> Path:
    return repo_root / "src" / "codex_autorunner" / "web_static" / MANIFEST_FILENAME


def translate_segment(segment: str) -> str:
    """Translate one SvelteKit route segment to its Starlette equivalent.

    Mirrors ``web_frontend/scripts/generate-spa-routes.mjs``:
      [foo]     -> {foo}
      [[foo]]   -> {foo}  (caller handles the "omitted" variant separately)
      [...foo]  -> {foo:path}
      literal   -> unchanged
    """
    m = _REST_SEGMENT.fullmatch(segment)
    if m:
        return f"{{{m.group(1).strip()}:path}}"
    m = _DYNAMIC_SEGMENT.fullmatch(segment)
    if m:
        return f"{{{m.group(1).strip()}}}"
    m = _OPTIONAL_SEGMENT.fullmatch(segment)
    if m:
        return f"{{{m.group(1).strip()}}}"
    return segment


def route_templates_for_page(rel_parent: Path) -> list[str]:
    """Starlette path template(s) implied by one +page.svelte's directory.

    Returns [] for the root page (rel_parent == "."), which the hub always
    redirects rather than serving a shell entry for. Trailing optional
    (``[[name]]``) segments expand into "with" and "without" variants.
    """
    raw_parts = list(rel_parent.parts)
    non_group = [p for p in raw_parts if not (p.startswith("(") and p.endswith(")"))]
    if not non_group:
        return []

    literal_parts = [translate_segment(p) for p in non_group]

    opt_run = 0
    for p in reversed(non_group):
        if _OPTIONAL_SEGMENT.fullmatch(p):
            opt_run += 1
        else:
            break

    if opt_run == 0:
        return ["/" + "/".join(literal_parts)]

    variants: list[str] = []
    for drop in range(0, opt_run + 1):
        kept = literal_parts[: len(literal_parts) - drop]
        if kept:
            variants.append("/" + "/".join(kept))
    return variants


def expected_route_templates(routes: Path) -> list[str]:
    """Recompute the full expected Starlette route-template set from disk."""
    if not routes.is_dir():
        raise FileNotFoundError(f"Web Hub routes directory missing: {routes}")

    templates: set[str] = set()
    for page in sorted(routes.rglob("+page.svelte")):
        if "node_modules" in page.parts:
            continue
        rel_parent = page.parent.relative_to(routes)
        if rel_parent == Path("."):
            continue  # root layout page; hub always redirects, checked separately
        templates.update(route_templates_for_page(rel_parent))
    return sorted(templates)


def load_manifest_routes(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    routes = data.get("routes") if isinstance(data, dict) else None
    if not isinstance(routes, list) or not all(isinstance(r, str) for r in routes):
        raise ValueError(f"Manifest at {path} has an unexpected shape")
    return sorted(routes)


def run_checks(*, repo_root: Path) -> tuple[list[str], list[str]]:
    """Return (errors, expected_templates)."""
    rd = routes_dir(repo_root)
    try:
        expected = expected_route_templates(rd)
    except FileNotFoundError as exc:
        return [str(exc)], []

    mp = manifest_path(repo_root)
    if not mp.exists():
        return (
            [
                f"Manifest not found at {mp}. Run `pnpm web:build` (or "
                "`pnpm --filter @codex-autorunner/web-hub build`) to generate it."
            ],
            expected,
        )

    try:
        actual = load_manifest_routes(mp)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"Failed to read manifest at {mp}: {exc}"], expected

    errors: list[str] = []
    expected_set = set(expected)
    actual_set = set(actual)
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    if missing:
        errors.append(
            "Manifest is missing routes present in web_frontend/src/routes/: "
            + ", ".join(missing)
        )
    if extra:
        errors.append(
            "Manifest has routes no longer present in web_frontend/src/routes/ "
            "(stale build): " + ", ".join(extra)
        )
    return errors, expected


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

    errors, expected = run_checks(repo_root=repo_root)
    if errors:
        print("Web Hub SPA route manifest check failed:", file=sys.stderr)
        for line in errors:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nFix: run `pnpm web:build` to regenerate "
            "src/codex_autorunner/web_static/spa_routes.json from the current "
            "web_frontend/src/routes/ tree.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Web Hub SPA route manifest OK ({repo_root}) — "
        f"{len(expected)} route templates in sync"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
