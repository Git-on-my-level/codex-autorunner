from __future__ import annotations

import json

from typer.testing import CliRunner

from codex_autorunner.surfaces.cli.cli import app


def test_chat_index_rebuild_dry_run_reports_projection_state(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "chat",
            "index",
            "rebuild",
            "--path",
            str(tmp_path),
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["projection"]["needs_rebuild"] is True
    assert payload["projection"]["row_count"] == 0


def test_chat_index_repair_stale_bound_archives_dry_run_reports_matches(
    tmp_path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "chat",
            "index",
            "repair-stale-bound-archives",
            "--path",
            str(tmp_path),
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["matched"] == 0
    assert payload["candidates"] == []
