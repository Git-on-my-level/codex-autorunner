from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from codex_autorunner.core.orchestration.legacy_backfill_gate import (
    LEGACY_ORCHESTRATION_BACKFILL_KEY,
    ensure_legacy_orchestration_backfill,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_thread_store import prepare_pma_thread_store


def test_ensure_legacy_orchestration_backfill_skips_work_when_marker_present(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO orch_legacy_backfill_flags (
                    backfill_key,
                    completed_at
                ) VALUES (?, '2026-04-06T00:00:00Z')
                """,
                (LEGACY_ORCHESTRATION_BACKFILL_KEY,),
            )

    with patch(
        "codex_autorunner.core.orchestration.legacy_backfill_gate."
        "backfill_legacy_thread_state",
    ) as mock_threads:
        ensure_legacy_orchestration_backfill(hub_root, durable=False)
        mock_threads.assert_not_called()


def test_prepare_pma_thread_store_runs_thread_backfill_once_without_marker(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    with patch(
        "codex_autorunner.core.orchestration.legacy_backfill_gate."
        "backfill_legacy_thread_state",
    ) as mock_threads:
        prepare_pma_thread_store(hub_root, durable=False)
        prepare_pma_thread_store(hub_root, durable=False)
        assert mock_threads.call_count == 1
