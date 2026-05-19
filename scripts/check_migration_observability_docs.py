#!/usr/bin/env python3
"""Keep PMA/chat migration observability docs aligned with code.

This check intentionally covers only the canonical PMA/chat/read-model paths
from the issue #1856 migration queue. It is not a route inventory.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RequiredLiteral:
    path: str
    literal: str
    reason: str


@dataclass(frozen=True)
class SourceRoute:
    path: str
    route_literal: str
    reason: str


DOC_LITERALS = (
    RequiredLiteral(
        "docs/ops/web-read-models.md",
        "/hub/read-models/chats",
        "canonical chat index snapshot route",
    ),
    RequiredLiteral(
        "docs/ops/web-read-models.md",
        "/hub/read-models/chats/<thread_id>",
        "canonical chat detail snapshot route",
    ),
    RequiredLiteral(
        "docs/ops/web-read-models.md",
        "/hub/read-models/chats/patches",
        "canonical chat index patch stream route",
    ),
    RequiredLiteral(
        "docs/ops/web-read-models.md",
        "/hub/read-models/chats/<thread_id>/patches",
        "canonical chat detail patch stream route",
    ),
    RequiredLiteral(
        "docs/ops/web-read-models.md",
        "/hub/pma/history/status",
        "PMA transcript mirror coverage route",
    ),
    RequiredLiteral(
        "docs/ops/web-read-models.md",
        "diagnostics/compatibility paths only",
        "legacy chat/PMA paths must be marked non-primary",
    ),
    RequiredLiteral(
        "docs/ops/web-read-models.md",
        ".venv/bin/python scripts/check_migration_observability_docs.py",
        "direct docs-code sync validation command",
    ),
    RequiredLiteral(
        "docs/ops/import-boundary-check.md",
        ".venv/bin/python scripts/check_import_boundaries.py",
        "documented import boundary validation command",
    ),
    RequiredLiteral(
        "docs/ops/hotspot-budget-guardrails.md",
        ".venv/bin/python -m pytest tests/test_hotspot_budgets.py -q",
        "documented hotspot budget validation command",
    ),
    RequiredLiteral(
        "docs/car_constitution/50_OBSERVABILITY_OPERATIONS.md",
        "projection.cursor_gap",
        "documented read-model repair diagnostic event",
    ),
    RequiredLiteral(
        "docs/ops/unified-automation-migration.md",
        "car doctor --json",
        "automation migration doctor gate command",
    ),
    RequiredLiteral(
        "docs/ops/unified-automation-migration.md",
        "car hub orchestration status --json",
        "hub automation migration diagnostic command",
    ),
    RequiredLiteral(
        "docs/ops/unified-automation-migration.md",
        "car pma automation migration-status --json",
        "PMA automation migration diagnostic command",
    ),
    RequiredLiteral(
        "docs/ops/release.md",
        "Automation migration gate",
        "release process includes automation migration gate",
    ),
    RequiredLiteral(
        "docs/ops/release.md",
        ".venv/bin/python scripts/check_migration_observability_docs.py",
        "release process includes migration observability docs-code sync",
    ),
)

SOURCE_ROUTES = (
    SourceRoute(
        "src/codex_autorunner/surfaces/web/routes/hub_chat_read_models.py",
        '"/hub/read-models/chats/{chat_id}"',
        "chat detail read-model route",
    ),
    SourceRoute(
        "src/codex_autorunner/surfaces/web/routes/hub_chat_read_models.py",
        '"/hub/read-models/chats/{chat_id}/patches"',
        "chat detail read-model patch stream route",
    ),
    SourceRoute(
        "src/codex_autorunner/surfaces/web/routes/hub_chat_read_models.py",
        'SNAPSHOT_CHAT_INDEX_ROUTE = "/hub/read-models/chats"',
        "chat index read-model route constant",
    ),
    SourceRoute(
        "src/codex_autorunner/surfaces/web/routes/hub_chat_read_models.py",
        'CHAT_INDEX_PATCH_ROUTE = "/hub/read-models/chats/patches"',
        "chat index read-model patch route constant",
    ),
    SourceRoute(
        "src/codex_autorunner/surfaces/web/routes/pma_routes/history_files_docs.py",
        '@router.get("/history/status")',
        "PMA transcript mirror coverage route",
    ),
    SourceRoute(
        "src/codex_autorunner/surfaces/web/routes/pma_routes/history_files_docs.py",
        '@router.get("/history/{turn_id}")',
        "PMA transcript mirror detail route",
    ),
)

LEGACY_PRIMARY_DOC_PATTERNS = (
    re.compile(
        r"primary[^.\n]*(/hub/chat/index|/hub/chat/threads/\{threadId\}/detail)",
        re.IGNORECASE,
    ),
    re.compile(
        r"primary[^.\n]*(/hub/pma/threads/\{(?:id|managed_thread_id)\}/(?:timeline|tail))",
        re.IGNORECASE,
    ),
)

PRIMARY_FRONTEND_SCAN_ROOTS = (
    Path("src/codex_autorunner/web_frontend/src/routes/chats"),
    Path("src/codex_autorunner/web_frontend/src/lib/application"),
    Path("src/codex_autorunner/web_frontend/src/lib/data"),
)
LEGACY_PRIMARY_FRONTEND_PATTERNS = (
    "/timeline?",
    "/tail",
    "/hub/chat/index",
    "/hub/chat/threads/",
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _iter_files(root: Path, suffixes: tuple[str, ...]) -> Iterable[Path]:
    if not root.exists():
        return ()
    return (
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in suffixes
    )


def check_docs(repo_root: Path) -> list[str]:
    errors: list[str] = []
    for item in DOC_LITERALS:
        path = repo_root / item.path
        if not path.exists():
            errors.append(f"{item.path}: missing doc for {item.reason}")
            continue
        text = _read_text(path)
        if item.literal not in text:
            errors.append(f"{item.path}: missing {item.literal!r} ({item.reason})")

    for path in (
        repo_root / "docs/ops/web-read-models.md",
        repo_root / "docs/architecture/web-ui-read-model-contracts.md",
    ):
        if not path.exists():
            continue
        text = _read_text(path)
        for pattern in LEGACY_PRIMARY_DOC_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                errors.append(
                    f"{path.relative_to(repo_root)}: legacy path is described as "
                    f"primary: {match.group(1)}"
                )
    return errors


def check_source_routes(repo_root: Path) -> list[str]:
    errors: list[str] = []
    for item in SOURCE_ROUTES:
        path = repo_root / item.path
        if not path.exists():
            errors.append(f"{item.path}: missing source for {item.reason}")
            continue
        text = _read_text(path)
        if item.route_literal not in text:
            errors.append(
                f"{item.path}: missing route literal {item.route_literal!r} "
                f"({item.reason})"
            )
    return errors


def check_primary_frontend_paths(repo_root: Path) -> list[str]:
    errors: list[str] = []
    for root in PRIMARY_FRONTEND_SCAN_ROOTS:
        abs_root = repo_root / root
        for path in _iter_files(abs_root, (".ts", ".svelte")):
            text = _read_text(path)
            for pattern in LEGACY_PRIMARY_FRONTEND_PATTERNS:
                if pattern in text:
                    errors.append(
                        f"{path.relative_to(repo_root)}: migrated chat primary "
                        f"surface still references legacy diagnostics path "
                        f"{pattern!r}"
                    )
    return errors


def run(repo_root: Path) -> list[str]:
    return [
        *check_docs(repo_root),
        *check_source_routes(repo_root),
        *check_primary_frontend_paths(repo_root),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    errors = run(args.repo_root.resolve())
    if errors:
        print("Migration observability docs-code sync failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Migration observability docs-code sync passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
