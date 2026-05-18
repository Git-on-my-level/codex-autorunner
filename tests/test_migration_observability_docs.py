from __future__ import annotations

from pathlib import Path

from scripts import check_migration_observability_docs as sync_check


def test_migration_observability_docs_code_sync_passes() -> None:
    assert sync_check.run(sync_check.REPO_ROOT) == []


def test_migration_observability_sync_rejects_legacy_primary_frontend_path(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    docs = repo_root / "docs"
    routes = repo_root / "src/codex_autorunner/surfaces/web/routes"
    frontend = repo_root / "src/codex_autorunner/web_frontend/src/routes/chats"
    (docs / "ops").mkdir(parents=True)
    (docs / "car_constitution").mkdir(parents=True)
    (routes / "pma_routes").mkdir(parents=True)
    frontend.mkdir(parents=True)

    (docs / "ops/web-read-models.md").write_text(
        "\n".join(
            [
                "/hub/read-models/chats",
                "/hub/read-models/chats/<thread_id>",
                "/hub/read-models/chats/patches",
                "/hub/read-models/chats/<thread_id>/patches",
                "/hub/pma/history/status",
                "diagnostics/compatibility paths only",
                ".venv/bin/python scripts/check_migration_observability_docs.py",
            ]
        ),
        encoding="utf-8",
    )
    (docs / "ops/import-boundary-check.md").write_text(
        ".venv/bin/python scripts/check_import_boundaries.py",
        encoding="utf-8",
    )
    (docs / "ops/hotspot-budget-guardrails.md").write_text(
        ".venv/bin/python -m pytest tests/test_hotspot_budgets.py -q",
        encoding="utf-8",
    )
    (docs / "car_constitution/50_OBSERVABILITY_OPERATIONS.md").write_text(
        "projection.cursor_gap",
        encoding="utf-8",
    )
    (routes / "hub_chat_read_models.py").write_text(
        "\n".join(
            [
                'SNAPSHOT_CHAT_INDEX_ROUTE = "/hub/read-models/chats"',
                'CHAT_INDEX_PATCH_ROUTE = "/hub/read-models/chats/patches"',
                '"some route /hub/read-models/chats/{chat_id}"',
                '"/hub/read-models/chats/{chat_id}/patches"',
            ]
        ),
        encoding="utf-8",
    )
    (routes / "pma_routes/history_files_docs.py").write_text(
        "\n".join(
            [
                '@router.get("/history/status")',
                '@router.get("/history/{turn_id}")',
            ]
        ),
        encoding="utf-8",
    )
    (frontend / "+page.svelte").write_text(
        "await api.get(`/hub/pma/threads/${id}/tail`);",
        encoding="utf-8",
    )

    errors = sync_check.run(repo_root)

    assert any("legacy diagnostics path" in error for error in errors)
